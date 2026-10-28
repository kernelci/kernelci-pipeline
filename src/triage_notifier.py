# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# Author: Yogesh Lal <yogesh.lal@oss.qualcomm.com>

"""Fire-and-forget trigger that hands failed LAVA jobs to a triage agent.

The scope here is deliberately narrow: decide whether a finished job deserves
triage, then POST the KernelCI node id and the LAVA job id to the agent and
return. The agent owns everything downstream — resolving the node through
kci-dev, fetching the log, the analysis itself, and delivering the report.
Keeping the KernelCI interaction on that side means this module needs no
KernelCI client, no credentials and no knowledge of report formats.

This code sits on the LAVA callback hot path, so it must never raise and
never block. `maybe_trigger` hands the work to a small private pool and
returns; inside that pool every failure ends as a log line plus a metric.

Configured entirely through TRIAGE_* environment variables. A no-op unless
TRIAGE_ENABLE=true and TRIAGE_AGENT_URL is set.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
        logger.warning(
            "%s=%r is not an integer; using default %d", name, raw, default
        )
        return default


class _NullMetrics:
    """Used when the caller hasn't supplied a real ``Metrics`` object."""

    def add(self, key: str, value: int) -> None:  # noqa: D401 — protocol match
        pass


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageConfig:
    enabled: bool
    agent_url: str
    submit_timeout_sec: int
    max_workers: int
    max_queue: int
    node_dedup_ttl_sec: int
    skip_infra_errors: bool
    max_per_hour: int  # 0 disables
    model: Optional[str]
    model_options: Optional[dict]

    @classmethod
    def from_env(cls) -> "TriageConfig":
        raw_opts = os.getenv("TRIAGE_MODEL_OPTIONS_JSON", "").strip()
        model_options: Optional[dict] = None
        if raw_opts:
            try:
                parsed = json.loads(raw_opts)
                if isinstance(parsed, dict):
                    model_options = parsed
                else:
                    logger.warning(
                        "TRIAGE_MODEL_OPTIONS_JSON is not a JSON object;"
                        " ignoring"
                    )
            except json.JSONDecodeError as exc:
                logger.warning("TRIAGE_MODEL_OPTIONS_JSON invalid: %s", exc)

        max_workers = max(1, _int_env("TRIAGE_MAX_WORKERS", 4))

        return cls(
            enabled=_bool_env("TRIAGE_ENABLE", False),
            agent_url=(os.getenv("TRIAGE_AGENT_URL") or "").rstrip("/"),
            # Only covers the submit POST. Nothing waits for the analysis,
            # so there is no second, longer deadline to keep in step.
            submit_timeout_sec=_int_env("TRIAGE_SUBMIT_TIMEOUT_SEC", 30),
            max_workers=max_workers,
            max_queue=_int_env("TRIAGE_MAX_QUEUE", max_workers * 4),
            node_dedup_ttl_sec=_int_env("TRIAGE_NODE_DEDUP_TTL_SEC", 3600),
            skip_infra_errors=_bool_env("TRIAGE_SKIP_INFRA_ERRORS", True),
            max_per_hour=max(0, _int_env("TRIAGE_MAX_PER_HOUR", 0)),
            model=os.getenv("TRIAGE_MODEL") or None,
            model_options=model_options,
        )


# --------------------------------------------------------------------------
# Triage-agent HTTP client
# --------------------------------------------------------------------------


class TriageAgentClient:
    """Submit-only client. The agent is authoritative once it has the job."""

    def __init__(
        self,
        base_url: str,
        timeout_sec: int,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_sec
        self._session = session or requests.Session()

    def submit(
        self,
        lava_job_id: str,
        node_id: str,
        model: Optional[str] = None,
        model_options: Optional[dict] = None,
    ) -> str:
        """Queue a triage run. Returns the agent's run_id. Raises on non-2xx.

        The LAVA job id is the URL path; the KernelCI node id travels in the
        body. Both are needed: the node id is what lets the agent pull the
        KernelCI-side picture, and the LAVA job id is its fallback route to
        the log when the node carries no stored artifact.
        """
        body: dict = {"node_id": node_id}
        if model:
            body["model"] = model
        if model_options:
            body["model_options"] = model_options

        resp = self._session.post(
            f"{self._base}/{lava_job_id}",
            json=body,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        run_id = resp.json().get("run_id")
        if not run_id:
            raise RuntimeError(
                f"triage-agent returned no run_id: {resp.text[:200]}"
            )
        return run_id


# --------------------------------------------------------------------------
# Notifier
# --------------------------------------------------------------------------


class TriageNotifier:
    """Trigger-only bridge from lava_callback to the triage agent."""

    def __init__(self, config: TriageConfig, metrics: Any = None) -> None:
        self._config = config
        self._metrics = metrics or _NullMetrics()
        self._lock = threading.Lock()
        self._dedup: dict = {}
        self._rate_window: deque = deque()
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._enabled = self._validate()
        self._pool: Optional[ThreadPoolExecutor] = None
        if self._enabled:
            self._pool = ThreadPoolExecutor(
                max_workers=config.max_workers,
                thread_name_prefix="triage-notifier",
            )

        self._agent = (
            TriageAgentClient(config.agent_url, config.submit_timeout_sec)
            if self._enabled
            else None
        )

    @classmethod
    def from_env(cls, metrics: Any = None) -> "TriageNotifier":
        return cls(TriageConfig.from_env(), metrics=metrics)

    # ---- setup helpers -------------------------------------------------

    def _validate(self) -> bool:
        if not self._config.enabled:
            logger.info("triage notifier disabled (TRIAGE_ENABLE unset)")
            return False
        if not self._config.agent_url:
            logger.warning(
                "TRIAGE_ENABLE=true but TRIAGE_AGENT_URL is empty; disabling"
            )
            return False
        logger.info(
            "triage notifier active: agent=%s workers=%d",
            self._config.agent_url,
            self._config.max_workers,
        )
        return True

    # ---- public entrypoint --------------------------------------------

    def maybe_trigger(
        self, job_node: dict, job_callback: Any, hierarchy: dict
    ) -> None:
        """Dispatch a triage run for this callback if the criteria match.

        Never raises. Returns immediately — the submit happens on the
        notifier's own pool.
        """
        if not self._enabled:
            return

        try:
            should, reason, extract = self._should_trigger(
                job_node,
                job_callback,
                hierarchy,
            )
            if not should:
                logger.info(
                    "triage skip node_id=%s reason=%s",
                    job_node.get("id"),
                    reason,
                )
                return

            node_id = extract["node_id"]

            # Check pool saturation BEFORE reserving a dedup / rate-limit
            # slot: a dropped run must not consume the 1h dedup TTL or a
            # max_per_hour token, otherwise legitimate retries stay
            # blocked for an hour after a burst.
            if self._pool is not None and self._pool_full():
                logger.warning(
                    "triage notifier saturated — dropping node_id=%s", node_id
                )
                self._metrics.add("triage_dropped_total", 1)
                return

            allowed, gate_reason = self._reserve_slot(node_id)
            if not allowed:
                logger.warning(
                    "triage dropped node_id=%s reason=%s", node_id, gate_reason
                )
                self._metrics.add(gate_reason, 1)
                return

            self._submit(extract)
        except Exception:  # noqa: BLE001 — never raise into the callback path
            logger.exception("triage_notifier.maybe_trigger internal error")

    # ---- decision logic ------------------------------------------------

    def _should_trigger(
        self, job_node: dict, job_callback: Any, hierarchy: dict
    ) -> tuple:
        extract = _extract_metadata(job_node)
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

    def _reserve_slot(self, node_id: str) -> tuple:
        """Combined dedup + rate-limit under one lock. On success the slot is
        consumed; the metric key on failure is returned as the reason so the
        caller can bump it verbatim.
        """
        now = time.monotonic()
        with self._lock:
            last = self._dedup.get(node_id)
            if (
                last is not None
                and now - last < self._config.node_dedup_ttl_sec
            ):
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
                self._dedup = {
                    k: v for k, v in self._dedup.items() if v > cutoff
                }
        return True, ""

    def _pool_full(self) -> bool:
        """Approximate backpressure using an atomic in-flight counter
        (queued + running): drop once the queued portion would exceed
        max_queue, allowing up to max_workers concurrently running.
        """
        with self._inflight_lock:
            return self._inflight > (
                self._config.max_workers + self._config.max_queue
            )

    def _submit(self, extract: dict) -> None:
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

    def _run(self, extract: dict) -> None:
        """One submit POST. Nothing is awaited beyond the 202."""
        node_id = extract["node_id"]
        lava_job_id = extract["lava_job_id"]
        assert self._agent is not None

        try:
            run_id = self._agent.submit(
                str(lava_job_id),
                node_id,
                model=self._config.model,
                model_options=self._config.model_options,
            )
        except (requests.RequestException, RuntimeError) as exc:
            logger.warning(
                "triage-agent submit failed for node_id=%s: %s", node_id, exc
            )
            self._metrics.add("triage_dispatch_failed_total", 1)
            return
        except Exception:  # noqa: BLE001
            logger.exception(
                "triage-agent submit crashed for node_id=%s", node_id
            )
            self._metrics.add("triage_dispatch_failed_total", 1)
            return

        self._metrics.add("triage_dispatched_total", 1)
        logger.info(
            "triage dispatched node_id=%s lava_job_id=%s run_id=%s",
            node_id,
            lava_job_id,
            run_id,
        )


# --------------------------------------------------------------------------
# Metadata extraction
# --------------------------------------------------------------------------


def _extract_metadata(job_node: dict) -> dict:
    """The two identifiers the agent needs. Everything else it can look up."""
    data = job_node.get("data") or {}
    return {
        "node_id": job_node.get("id"),
        "lava_job_id": data.get("job_id"),
    }


def _has_failing_test(hierarchy: dict) -> bool:
    def walk(children: list) -> bool:
        for child in children or []:
            node = child.get("node") or {}
            if node.get("kind") == "test" and node.get("result") == "fail":
                return True
            if walk(child.get("child_nodes") or []):
                return True
        return False

    return walk(hierarchy.get("child_nodes") or [])
