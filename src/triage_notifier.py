# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# Author: Yogesh Lal <yogesh.lal@oss.qualcomm.com>

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Optional

import requests


logger = logging.getLogger("triage_notifier")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using default %d", name, raw, default)
        return default


def _list_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


class _NullMetrics:
    """Fallback used when the caller hasn't supplied a real ``Metrics`` object."""

    def add(self, key: str, value: int) -> None:  # noqa: D401 — protocol match
        pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TriageConfig:
    enabled: bool
    agent_url: str
    agent_timeout_sec: int
    poll_interval_sec: int
    max_workers: int
    max_queue: int
    node_dedup_ttl_sec: int
    skip_infra_errors: bool
    max_per_hour: int   # 0 disables
    model: Optional[str]
    model_options: Optional[dict[str, Any]]
    kernelci_web_url: Optional[str]

    github_repo: Optional[str]
    github_token: Optional[str]
    github_labels: list[str]

    smtp_host: Optional[str]
    smtp_port: int
    smtp_user: Optional[str]
    smtp_password: Optional[str]
    smtp_from: Optional[str]
    email_to: list[str]
    email_cc: list[str]

    @classmethod
    def from_env(cls) -> "TriageConfig":
        raw_opts = os.getenv("TRIAGE_MODEL_OPTIONS_JSON", "").strip()
        model_options: Optional[dict[str, Any]] = None
        if raw_opts:
            try:
                parsed = json.loads(raw_opts)
                if isinstance(parsed, dict):
                    model_options = parsed
                else:
                    logger.warning("TRIAGE_MODEL_OPTIONS_JSON is not a JSON object; ignoring")
            except json.JSONDecodeError as exc:
                logger.warning("TRIAGE_MODEL_OPTIONS_JSON invalid: %s", exc)

        max_workers = max(1, _int_env("TRIAGE_MAX_WORKERS", 4))

        return cls(
            enabled=_bool_env("TRIAGE_ENABLE", False),
            agent_url=(os.getenv("TRIAGE_AGENT_URL") or "").rstrip("/"),
            # Must exceed the agent's own TRIAGE_RUN_TIMEOUT_SEC (default
            # 1800s) plus submit + log-fetch overhead. When the two are
            # equal the client deadline always fires first (the notifier
            # clock starts before the agent's), so operators see the
            # synthesised "timeout" instead of the specific server-side
            # "failed" status. 2100s = 1800s + 5min slack.
            agent_timeout_sec=_int_env("TRIAGE_AGENT_TIMEOUT_SEC", 2100),
            poll_interval_sec=max(1, _int_env("TRIAGE_POLL_INTERVAL_SEC", 15)),
            max_workers=max_workers,
            max_queue=_int_env("TRIAGE_MAX_QUEUE", max_workers * 4),
            node_dedup_ttl_sec=_int_env("TRIAGE_NODE_DEDUP_TTL_SEC", 3600),
            skip_infra_errors=_bool_env("TRIAGE_SKIP_INFRA_ERRORS", True),
            max_per_hour=max(0, _int_env("TRIAGE_MAX_PER_HOUR", 0)),
            model=os.getenv("TRIAGE_MODEL") or None,
            model_options=model_options,
            kernelci_web_url=(os.getenv("TRIAGE_KERNELCI_WEB_URL") or "").rstrip("/") or None,
            github_repo=os.getenv("TRIAGE_GITHUB_REPO") or None,
            github_token=os.getenv("TRIAGE_GITHUB_TOKEN") or None,
            github_labels=_list_env("TRIAGE_GITHUB_LABELS") or ["triage", "lava-failure"],
            smtp_host=os.getenv("TRIAGE_SMTP_HOST") or None,
            smtp_port=_int_env("TRIAGE_SMTP_PORT", 587),
            smtp_user=os.getenv("TRIAGE_SMTP_USER") or None,
            smtp_password=os.getenv("TRIAGE_SMTP_PASSWORD") or None,
            smtp_from=os.getenv("TRIAGE_SMTP_FROM") or None,
            email_to=_list_env("TRIAGE_EMAIL_TO"),
            email_cc=_list_env("TRIAGE_EMAIL_CC"),
        )


@dataclass(frozen=True)
class TriageContext:
    report: str
    status: str          # completed | failed | timeout
    error: Optional[str]
    node_id: str
    lava_job_id: str
    kernel_tree: Optional[str]
    kernel_branch: Optional[str]
    kernel_commit: Optional[str]
    device_type: Optional[str]
    lava_log_url: Optional[str]
    kernelci_web_url: Optional[str]
    triage_run_id: Optional[str]
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------
# Triage-agent HTTP client
# --------------------------------------------------------------------------

class TriageAgentClient:
    def __init__(self, base_url: str, poll_interval_sec: int,
                 session: Optional[requests.Session] = None) -> None:
        self._base = base_url.rstrip("/")
        self._poll_interval = poll_interval_sec
        self._session = session or requests.Session()

    def submit(self, lava_job_id: str, model: Optional[str],
               model_options: Optional[dict[str, Any]],
               log_url: Optional[str] = None) -> str:
        """Returns the triage run_id. Raises on non-2xx."""
        body: dict[str, Any] = {}
        if model:
            body["model"] = model
        if model_options:
            body["model_options"] = model_options
        if log_url:
            body["log_url"] = log_url

        resp = self._session.post(
            f"{self._base}/{lava_job_id}",
            json=body or None,
            timeout=30,
        )
        resp.raise_for_status()
        run_id = resp.json().get("run_id")
        if not run_id:
            raise RuntimeError(f"triage-agent returned no run_id: {resp.text[:200]}")
        return run_id

    def poll_until_done(self, run_id: str, deadline_monotonic: float) -> dict[str, Any]:
        """Poll until status is completed/failed or deadline lapses.

        Returns the last observed status payload, augmented with
        ``status="timeout"`` on deadline expiry.
        """
        last: dict[str, Any] = {}
        while True:
            if time.monotonic() > deadline_monotonic:
                last["status"] = "timeout"
                last.setdefault("error", "poll deadline exceeded")
                return last
            try:
                resp = self._session.get(
                    f"{self._base}/triage/{run_id}",
                    timeout=30,
                )
                resp.raise_for_status()
                last = resp.json()
            except requests.RequestException as exc:
                logger.warning("triage-agent poll error for %s: %s", run_id, exc)
                # Fall through and retry after sleep, honouring the deadline.
            status = last.get("status")
            if status in ("completed", "failed"):
                return last
            time.sleep(self._poll_interval)


# --------------------------------------------------------------------------
# Delivery: GitHub
# --------------------------------------------------------------------------

class GithubDelivery:
    _API = "https://api.github.com"

    def __init__(self, repo: str, token: str, labels: list[str],
                 session: Optional[requests.Session] = None) -> None:
        self._repo = repo
        self._session = session or requests.Session()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._labels = labels

    def deliver(self, ctx: TriageContext) -> str:
        """Returns 'created', 'commented', or 'failed'."""
        body = _format_report_body(ctx)
        title = _format_subject(ctx)

        existing = self._find_open_issue(ctx.node_id)
        if existing is not None:
            ok = self._post_comment(existing, body)
            return "commented" if ok else "failed"
        ok = self._create_issue(title, body)
        return "created" if ok else "failed"

    def _find_open_issue(self, node_id: str) -> Optional[int]:
        q = f"repo:{self._repo} is:issue is:open in:body {node_id}"
        try:
            resp = self._request("GET", f"{self._API}/search/issues", params={"q": q})
        except requests.RequestException as exc:
            logger.warning("github search failed for node_id=%s: %s", node_id, exc)
            return None
        if resp.status_code >= 400:
            logger.warning("github search %s: %s", resp.status_code, resp.text[:200])
            return None
        items = resp.json().get("items", [])
        return items[0]["number"] if items else None

    def _create_issue(self, title: str, body: str) -> bool:
        payload = {"title": title, "body": body, "labels": self._labels}
        try:
            resp = self._request(
                "POST", f"{self._API}/repos/{self._repo}/issues", json=payload,
            )
        except requests.RequestException as exc:
            logger.warning("github create issue error: %s", exc)
            return False
        if resp.status_code >= 400:
            logger.warning("github create issue %s: %s", resp.status_code, resp.text[:200])
            return False
        logger.info("github issue created: %s", resp.json().get("html_url"))
        return True

    def _post_comment(self, issue_number: int, body: str) -> bool:
        payload = {"body": body}
        try:
            resp = self._request(
                "POST",
                f"{self._API}/repos/{self._repo}/issues/{issue_number}/comments",
                json=payload,
            )
        except requests.RequestException as exc:
            logger.warning("github comment error on #%d: %s", issue_number, exc)
            return False
        if resp.status_code >= 400:
            logger.warning("github comment #%d %s: %s",
                           issue_number, resp.status_code, resp.text[:200])
            return False
        logger.info("github comment posted on #%d", issue_number)
        return True

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        """Single retry on 5xx/429 with 5s sleep."""
        for attempt in (1, 2):
            resp = self._session.request(
                method, url, headers=self._headers, timeout=30, **kwargs,
            )
            if attempt == 1 and (resp.status_code >= 500 or resp.status_code == 429):
                logger.info("github %s %s → %d; retrying in 5s",
                            method, url, resp.status_code)
                time.sleep(5)
                continue
            return resp
        return resp  # type: ignore[return-value] — unreachable in practice


# --------------------------------------------------------------------------
# Delivery: Email
# --------------------------------------------------------------------------

class EmailDelivery:
    def __init__(self, host: str, port: int, user: Optional[str],
                 password: Optional[str], sender: str,
                 to: list[str], cc: list[str]) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._from = sender
        self._to = to
        self._cc = cc

    def deliver(self, ctx: TriageContext) -> bool:
        msg = EmailMessage()
        msg["Subject"] = _format_subject(ctx)
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        if self._cc:
            msg["Cc"] = ", ".join(self._cc)

        plain = _format_report_body(ctx)
        msg.set_content(plain)

        html = _render_html(plain)
        if html is not None:
            msg.add_alternative(html, subtype="html")

        try:
            if self._port == 465:
                ctx_tls = ssl.create_default_context()
                with smtplib.SMTP_SSL(self._host, self._port, context=ctx_tls,
                                      timeout=30) as smtp:
                    self._maybe_login(smtp)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=30) as smtp:
                    smtp.ehlo()
                    if self._port == 587 or smtp.has_extn("starttls"):
                        smtp.starttls(context=ssl.create_default_context())
                        smtp.ehlo()
                    self._maybe_login(smtp)
                    smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning("smtp send failed: %s", exc)
            return False
        logger.info("triage email sent to %s", msg["To"])
        return True

    def _maybe_login(self, smtp: smtplib.SMTP) -> None:
        if self._user and self._password:
            smtp.login(self._user, self._password)


def _render_html(markdown_text: str) -> Optional[str]:
    try:
        import markdown  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        return markdown.markdown(
            markdown_text,
            extensions=["fenced_code", "tables"],
        )
    except Exception as exc:  # noqa: BLE001 — optional dep, don't crash delivery
        logger.warning("markdown render failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Formatting shared between GitHub and email
# --------------------------------------------------------------------------

def _format_subject(ctx: TriageContext) -> str:
    tree = ctx.kernel_tree or "unknown-tree"
    branch = ctx.kernel_branch or "unknown-branch"
    commit = (ctx.kernel_commit or "")[:12] or "unknown"
    device = ctx.device_type or "unknown-device"
    return (f"[triage] {tree}/{branch}@{commit} — "
            f"LAVA job {ctx.lava_job_id} on {device}")


def _format_report_body(ctx: TriageContext) -> str:
    lines = [
        "| Field | Value |",
        "|---|---|",
        f"| node_id | `{ctx.node_id}` |",
        f"| LAVA job | `{ctx.lava_job_id}` |",
        f"| tree/branch | `{ctx.kernel_tree or '?'}` / `{ctx.kernel_branch or '?'}` |",
        f"| commit | `{ctx.kernel_commit or '?'}` |",
        f"| device | `{ctx.device_type or '?'}` |",
        f"| triage status | `{ctx.status}` |",
        f"| triage run_id | `{ctx.triage_run_id or '-'}` |",
        f"| generated | {ctx.started_at.isoformat(timespec='seconds')} |",
    ]
    if ctx.lava_log_url:
        lines.append(f"| LAVA log | {ctx.lava_log_url} |")
    if ctx.kernelci_web_url:
        lines.append(f"| KernelCI node | {ctx.kernelci_web_url}/node/{ctx.node_id} |")

    body = ["## Triage report", "", *lines, ""]
    if ctx.status == "completed" and ctx.report:
        body.extend(["## Analysis", "", ctx.report])
    else:
        body.extend([
            "## Analysis unavailable",
            "",
            f"Triage did not produce a report: **{ctx.status}**.",
            f"Error: `{ctx.error or 'no detail'}`",
        ])
    return "\n".join(body)


# --------------------------------------------------------------------------
# Notifier
# --------------------------------------------------------------------------

class TriageNotifier:
    """Fire-and-forget bridge from lava_callback to triage-agent."""

    def __init__(self, config: TriageConfig, metrics: Any = None) -> None:
        self._config = config
        self._metrics = metrics or _NullMetrics()
        self._lock = threading.Lock()
        self._dedup: dict[str, float] = {}
        self._rate_window: deque[float] = deque()
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._enabled = self._validate()
        self._pool: Optional[ThreadPoolExecutor] = None
        if self._enabled:
            self._pool = ThreadPoolExecutor(
                max_workers=config.max_workers,
                thread_name_prefix="triage-notifier",
            )

        self._agent = (TriageAgentClient(config.agent_url, config.poll_interval_sec)
                       if self._enabled else None)
        self._github = self._build_github()
        self._email = self._build_email()

    @classmethod
    def from_env(cls, metrics: Any = None) -> "TriageNotifier":
        return cls(TriageConfig.from_env(), metrics=metrics)

    # ---- setup helpers -------------------------------------------------

    def _validate(self) -> bool:
        if not self._config.enabled:
            logger.info("triage notifier disabled (TRIAGE_ENABLE unset)")
            return False
        if not self._config.agent_url:
            logger.warning("TRIAGE_ENABLE=true but TRIAGE_AGENT_URL is empty; disabling")
            return False
        logger.info("triage notifier active: agent=%s workers=%d",
                    self._config.agent_url, self._config.max_workers)
        return True

    def _build_github(self) -> Optional[GithubDelivery]:
        if not self._enabled:
            return None
        if not (self._config.github_repo and self._config.github_token):
            logger.info("github delivery disabled (repo/token not set)")
            return None
        return GithubDelivery(
            self._config.github_repo,
            self._config.github_token,
            self._config.github_labels,
        )

    def _build_email(self) -> Optional[EmailDelivery]:
        if not self._enabled:
            return None
        if not (self._config.smtp_host and self._config.smtp_from
                and self._config.email_to):
            logger.info("email delivery disabled (host/from/to not fully set)")
            return None
        return EmailDelivery(
            self._config.smtp_host,
            self._config.smtp_port,
            self._config.smtp_user,
            self._config.smtp_password,
            self._config.smtp_from,
            self._config.email_to,
            self._config.email_cc,
        )

    # ---- public entrypoint --------------------------------------------

    def maybe_trigger(self, job_node: dict, job_callback: Any,
                      hierarchy: dict) -> None:
        """Dispatch a triage run for this callback if the criteria match.

        Never raises. Returns immediately — the actual work happens on the
        notifier's own pool.
        """
        if not self._enabled:
            return

        try:
            should, reason, extract = self._should_trigger(
                job_node, job_callback, hierarchy,
            )
            if not should:
                logger.info("triage skip node_id=%s reason=%s",
                            job_node.get("id"), reason)
                return

            node_id = extract["node_id"]

            # Check pool saturation BEFORE reserving a dedup / rate-limit
            # slot: a dropped run must not consume the 1h dedup TTL or a
            # max_per_hour token, otherwise legitimate retries stay
            # blocked for an hour after a burst.
            if self._pool is not None and self._pool_full():
                logger.warning("triage notifier saturated — dropping node_id=%s",
                               node_id)
                self._metrics.add("triage_dropped_total", 1)
                return

            allowed, gate_reason = self._reserve_slot(node_id)
            if not allowed:
                logger.warning("triage dropped node_id=%s reason=%s",
                               node_id, gate_reason)
                self._metrics.add(gate_reason, 1)
                return

            assert self._pool is not None
            self._submit(extract)
        except Exception:  # noqa: BLE001 — must never raise into the callback path
            logger.exception("triage_notifier.maybe_trigger internal error")

    # ---- decision logic ------------------------------------------------

    def _should_trigger(self, job_node: dict, job_callback: Any,
                        hierarchy: dict) -> tuple[bool, str, dict[str, Any]]:
        extract = _extract_metadata(job_node, self._config.kernelci_web_url)
        if not extract["node_id"]:
            return False, "missing node_id", extract
        if not extract["lava_job_id"]:
            return False, "missing lava job_id", extract

        result = job_node.get("result")
        has_failing = _has_failing_test(hierarchy)

        if result == "fail":
            return True, "job result=fail", extract
        if result == "incomplete":
            infra = False
            try:
                infra = bool(job_callback.is_infra_error())
            except Exception:  # noqa: BLE001 — callback API is out of our control
                infra = False
            if infra and self._config.skip_infra_errors:
                return False, "incomplete due to infra error (skipped)", extract
            return True, "job result=incomplete", extract
        if has_failing:
            return True, "descendant test failure", extract
        return False, f"no trigger (result={result})", extract

    def _reserve_slot(self, node_id: str) -> tuple[bool, str]:
        """Combined dedup + rate-limit under one lock. On success the slot is
        consumed; the metric key on failure is returned as the reason so the
        caller can bump it verbatim.
        """
        now = time.monotonic()
        with self._lock:
            last = self._dedup.get(node_id)
            if last is not None and now - last < self._config.node_dedup_ttl_sec:
                return False, "triage_dropped_total"

            if self._config.max_per_hour:
                while self._rate_window and now - self._rate_window[0] > 3600:
                    self._rate_window.popleft()
                if len(self._rate_window) >= self._config.max_per_hour:
                    return False, "triage_rate_limited_total"
                self._rate_window.append(now)

            self._dedup[node_id] = now
            if len(self._dedup) > 10000:
                cutoff = now - self._config.node_dedup_ttl_sec
                self._dedup = {k: v for k, v in self._dedup.items() if v > cutoff}
        return True, ""

    def _pool_full(self) -> bool:
        """Approximate backpressure using an atomic in-flight counter
        (queued + running). Preserves the legacy semantics of the private
        _work_queue.qsize() check: drop once the queued portion would
        exceed max_queue, allowing up to max_workers concurrently running.
        """
        with self._inflight_lock:
            return self._inflight > self._config.max_workers + self._config.max_queue

    def _submit(self, extract: dict[str, Any]) -> None:
        """Submit to the pool while tracking inflight count for _pool_full."""
        assert self._pool is not None
        with self._inflight_lock:
            self._inflight += 1
        try:
            future = self._pool.submit(self._run, extract)
        except Exception:
            with self._inflight_lock:
                self._inflight -= 1
            raise
        future.add_done_callback(self._on_done)

    def _on_done(self, _future) -> None:
        with self._inflight_lock:
            self._inflight -= 1

    # ---- worker --------------------------------------------------------

    def _run(self, extract: dict[str, Any]) -> None:
        node_id = extract["node_id"]
        lava_job_id = extract["lava_job_id"]
        logger.info("triage dispatch node_id=%s lava_job_id=%s", node_id, lava_job_id)

        assert self._agent is not None

        run_id: Optional[str] = None
        try:
            run_id = self._agent.submit(
                str(lava_job_id),
                self._config.model,
                self._config.model_options,
                log_url=extract.get("lava_log_url"),
            )
            self._metrics.add("triage_dispatched_total", 1)
        except (requests.RequestException, RuntimeError) as exc:
            logger.warning("triage-agent submit failed for node_id=%s: %s",
                           node_id, exc)
            self._metrics.add("triage_dispatch_failed_total", 1)
            return
        except Exception:  # noqa: BLE001
            logger.exception("triage-agent submit crashed for node_id=%s", node_id)
            self._metrics.add("triage_dispatch_failed_total", 1)
            return

        deadline = time.monotonic() + self._config.agent_timeout_sec
        try:
            payload = self._agent.poll_until_done(run_id, deadline)
        except Exception:  # noqa: BLE001
            logger.exception("triage-agent poll crashed for run_id=%s", run_id)
            payload = {"status": "failed", "error": "poll crashed"}

        status = payload.get("status") or "failed"
        if status == "completed":
            self._metrics.add("triage_completed_total", 1)
        elif status == "timeout":
            self._metrics.add("triage_timeout_total", 1)
        else:
            self._metrics.add("triage_failed_total", 1)

        ctx = TriageContext(
            report=payload.get("report") or "",
            status=status,
            error=payload.get("error"),
            node_id=node_id,
            lava_job_id=str(lava_job_id),
            kernel_tree=extract.get("kernel_tree"),
            kernel_branch=extract.get("kernel_branch"),
            kernel_commit=extract.get("kernel_commit"),
            device_type=extract.get("device_type"),
            lava_log_url=extract.get("lava_log_url"),
            kernelci_web_url=self._config.kernelci_web_url,
            triage_run_id=run_id,
        )

        self._deliver_github(ctx)
        self._deliver_email(ctx)

    def _deliver_github(self, ctx: TriageContext) -> None:
        if self._github is None:
            return
        try:
            outcome = self._github.deliver(ctx)
        except Exception:  # noqa: BLE001
            logger.exception("github delivery crashed")
            outcome = "failed"
        if outcome == "created":
            self._metrics.add("triage_github_created_total", 1)
        elif outcome == "commented":
            self._metrics.add("triage_github_comment_total", 1)
        else:
            self._metrics.add("triage_github_failed_total", 1)

    def _deliver_email(self, ctx: TriageContext) -> None:
        if self._email is None:
            return
        try:
            ok = self._email.deliver(ctx)
        except Exception:  # noqa: BLE001
            logger.exception("email delivery crashed")
            ok = False
        if ok:
            self._metrics.add("triage_email_sent_total", 1)
        else:
            self._metrics.add("triage_email_failed_total", 1)


# --------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------

def _extract_metadata(job_node: dict, kernelci_web_url: Optional[str]) -> dict[str, Any]:
    data = job_node.get("data") or {}
    rev = data.get("kernel_revision") or {}
    artifacts = job_node.get("artifacts") or {}
    return {
        "node_id": job_node.get("id"),
        "lava_job_id": data.get("job_id"),
        "kernel_tree": rev.get("tree"),
        "kernel_branch": rev.get("branch"),
        "kernel_commit": rev.get("commit"),
        "device_type": data.get("platform"),
        "lava_log_url": artifacts.get("lava_log"),
    }


def _has_failing_test(hierarchy: dict) -> bool:
    def walk(children: list[dict]) -> bool:
        for child in children or []:
            node = child.get("node") or {}
            if node.get("kind") == "test" and node.get("result") == "fail":
                return True
            if walk(child.get("child_nodes") or []):
                return True
        return False
    return walk(hierarchy.get("child_nodes") or [])
