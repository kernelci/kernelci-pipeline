#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>
#
# The forecast graph walk mirrors build_forecast_graph() in
# kernelci-core (kernelci/cli/config.py) by Guillaume Tucker and
# Denys Fedoryshchenko, re-rooted at a real checkout node.
"""
Validate Maestro scheduling against the pipeline forecast.

`kci config forecast` (kernelci-core) predicts, from the merged pipeline
YAML, which kbuilds a checkout should produce and which test jobs each
kbuild should trigger.  This script turns that prediction into a daily
audit of what actually ran:

  * fetch checkout nodes old enough for their whole job tree to have
    settled (at least --age-hours old, over a --window-hours period);
  * for each checkout, compare the actual kbuild child nodes against the
    forecast built with the checkout's real kernel revision;
  * for each kbuild that passed, compare its actual test job child nodes
    against the forecast.

A missing child is only a finding when its parent succeeded: a failed
checkout produces no kbuilds and a failed kbuild triggers no test jobs,
so those absences are expected and are just counted.  Nodes that exist
but are not in the forecast are reported as unexpected (forecast or
rules drift).

Finding categories (actionable, in `findings`):
  * MISSING_KBUILD    - checkout passed, forecast expects the kbuild,
                        no such node exists.
  * MISSING_JOB       - kbuild passed (or checkout passed for jobs
                        triggered directly by the checkout), forecast
                        expects the job, no such node exists.
  * UNEXPECTED_KBUILD - kbuild node exists but the forecast does not
                        predict it.
  * UNEXPECTED_JOB    - job node exists but the forecast does not
                        predict it (e.g. rules or runtime drift).

Informational (counted, listed in the JSON report only):
  * external nodes      - owner differs from the checkout owner:
                          external submitters (e.g. qualcomm) schedule
                          their own jobs under Maestro kbuilds, outside
                          this pipeline's forecast.
  * non-scheduler nodes - no scheduler entry creates this job from this
                          parent: nodes created dynamically by job
                          runners, e.g. the per-suite kselftest build
                          nodes kbuild.py attaches to kbuilds.

Validating old checkouts against today's config flags jobs added to the
config since then as MISSING; run against the pipeline config as it was
at the start of the window (the GitHub workflow does this) to avoid it.

Exit status is 1 when --fail-on-miss is given and any MISSING_* finding
was recorded.
"""

import argparse
import datetime
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_yaml import merge_files  # noqa: E402

MISSING_CATEGORIES = ("MISSING_KBUILD", "MISSING_JOB")
UNEXPECTED_CATEGORIES = ("UNEXPECTED_KBUILD", "UNEXPECTED_JOB")


def import_forecast(core_dir):
    """Import the forecast entry evaluator from kernelci-core"""
    if core_dir:
        sys.path.insert(0, core_dir)
    try:
        from kernelci.cli.config import evaluate_forecast_entry
    except ImportError as exc:
        sys.exit(
            "Cannot import evaluate_forecast_entry from kernelci-core: "
            f"{exc}\n"
            "Install a kernelci-core tree with forecast graph support "
            "and point --core-dir at it (or add it to PYTHONPATH)."
        )
    return evaluate_forecast_entry


class MaestroAPI:
    """Read-only Maestro API client with pagination"""

    def __init__(self, url, timeout=60, page_size=1000):
        self.base = url.rstrip("/")
        self.timeout = timeout
        self.page_size = page_size
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            max_retries=requests.adapters.Retry(
                total=3, backoff_factor=2, status_forcelist=[500, 502, 504]
            )
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def nodes(self, **filters):
        """Fetch all nodes matching the filters, following pagination"""
        items = []
        offset = 0
        while True:
            params = dict(filters)
            params.update({"limit": self.page_size, "offset": offset})
            resp = self.session.get(
                f"{self.base}/latest/nodes",
                params=params,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            page = data.get("items", [])
            items.extend(page)
            offset += len(page)
            if not page or offset >= data.get("total", 0):
                break
        return items


def event_source_name(event):
    """Job name whose nodes feed a scheduler entry (checkout for root)"""
    if event.get("kind") == "checkout":
        return event.get("name", "checkout")
    return event.get("name")


def node_signature(node):
    return json.dumps(node, sort_keys=True)


def build_expected_graph(evaluate_entry, merged_data, checkout_cfg, root_node):
    """Build the forecast graph rooted at a real checkout node.

    Returns {source job name: {(child name, platform, kind, runtime)}}
    where 'checkout' is the root source.  A platform of None means the
    scheduler entry has no platform list (e.g. docker/kubernetes jobs).
    """
    nodes = {"checkout": [root_node]}
    signatures = {"checkout": {node_signature(root_node)}}
    edges = {}
    scheduler_entries = merged_data.get("scheduler", [])

    changed = True
    while changed:
        changed = False
        for entry in scheduler_entries:
            source = event_source_name(entry.get("event", {}))
            if not source:
                continue
            for input_node in list(nodes.get(source, [])):
                children, _ = evaluate_entry(
                    merged_data, entry, input_node, checkout_cfg
                )
                for child in children:
                    name = child["name"]
                    edges.setdefault(source, set()).add(
                        (
                            name,
                            child["data"].get("platform"),
                            child["kind"],
                            child["data"].get("runtime"),
                        )
                    )
                    sig = node_signature(child)
                    sigs = signatures.setdefault(name, set())
                    if sig in sigs:
                        continue
                    sigs.add(sig)
                    nodes.setdefault(name, []).append(child)
                    changed = True

    return edges


def scheduler_pairs(merged_data):
    """Set of (source job name, job name) pairs the scheduler can create.

    Used to tell scheduler-created children apart from nodes attached
    dynamically by job runners (e.g. per-suite kselftest build nodes).
    """
    pairs = set()
    for entry in merged_data.get("scheduler", []):
        source = event_source_name(entry.get("event", {}))
        if source and entry.get("job"):
            pairs.add((source, entry["job"]))
    return pairs


def checkout_build_config(merged_data, tree, branch):
    """Merge the build_configs entries matching a tree/branch.

    Multiple entries can point at the same tree/branch with different
    architecture filters; the forecast should expect the union.
    """
    matches = [
        cfg
        for cfg in merged_data.get("build_configs", {}).values()
        if cfg.get("tree") == tree and cfg.get("branch") == branch
    ]
    if not matches:
        return None
    architectures = set()
    for cfg in matches:
        if not cfg.get("architectures"):
            architectures = None
            break
        architectures.update(cfg["architectures"])
    merged = dict(matches[0])
    merged["architectures"] = sorted(architectures) if architectures else None
    return merged


def match_children(expected, actual):
    """Compare forecast {(name, platform): runtime} with actual children.

    A platform of None means any platform.  Returns (missing,
    unexpected) where missing is a list of (name, platform) keys and
    unexpected a list of node dicts.
    """
    actual_pairs = [
        (node.get("name"), node.get("data", {}).get("platform"), node)
        for node in actual
    ]
    missing = []
    for name, platform in sorted(
        expected, key=lambda item: (item[0], item[1] or "")
    ):
        if platform is None:
            found = any(a_name == name for a_name, _, _ in actual_pairs)
        else:
            found = any(
                a_name == name and a_platform == platform
                for a_name, a_platform, _ in actual_pairs
            )
        if not found:
            missing.append((name, platform))

    expected_names = {name for name, platform in expected if platform is None}
    unexpected = [
        node
        for a_name, a_platform, node in actual_pairs
        if a_name not in expected_names and (a_name, a_platform) not in expected
    ]
    return missing, unexpected


def node_ok(node):
    return node.get("result") == "pass"


def node_settled(node):
    return node.get("state") == "done"


def describe_node(node):
    data = node.get("data", {})
    parts = [f"{node.get('name')} ({node.get('id')})"]
    if data.get("platform"):
        parts.append(f"platform={data['platform']}")
    if data.get("runtime"):
        parts.append(f"runtime={data['runtime']}")
    if node.get("owner"):
        parts.append(f"owner={node['owner']}")
    parts.append(f"result={node.get('result')}")
    return " ".join(parts)


def _checkout_skip_reason(merged_data, checkout):
    """Reason not to validate a checkout at all, or None"""
    if checkout.get("jobfilter") or checkout.get("platform_filter"):
        return "custom checkout with job/platform filter"
    revision = checkout.get("data", {}).get("kernel_revision", {})
    version = revision.get("version") or {}
    if version.get("version") is None or version.get("patchlevel") is None:
        return "checkout has no parsed kernel version"
    if not checkout_build_config(
        merged_data, revision.get("tree"), revision.get("branch")
    ):
        return "no build_config for this tree/branch"
    return None


def _checkout_forecast(merged_data, evaluate_entry, checkout, graph_cache):
    """Forecast edges for a checkout, cached per tree/branch/version"""
    revision = checkout["data"]["kernel_revision"]
    version = revision["version"]
    cache_key = (
        revision["tree"],
        revision["branch"],
        version["version"],
        version["patchlevel"],
    )
    edges = graph_cache.get(cache_key)
    if edges is None:
        build_config = checkout_build_config(
            merged_data, revision["tree"], revision["branch"]
        )
        root_node = {
            "kind": "checkout",
            "name": "checkout",
            "data": {"kernel_revision": revision},
        }
        edges = build_expected_graph(
            evaluate_entry, merged_data, build_config, root_node
        )
        graph_cache[cache_key] = edges
    return edges


def _fetch_children(api, checkout):
    """Fetch kbuild children and all job nodes grouped by parent ID"""
    revision = checkout["data"]["kernel_revision"]
    filters = {
        "data.kernel_revision.commit": revision["commit"],
        "data.kernel_revision.tree": revision["tree"],
        "data.kernel_revision.branch": revision["branch"],
    }
    kbuilds = [
        node
        for node in api.nodes(kind="kbuild", **filters)
        if node.get("parent") == checkout["id"]
    ]
    jobs_by_parent = {}
    for job in api.nodes(kind="job", **filters):
        jobs_by_parent.setdefault(job.get("parent"), []).append(job)
    return kbuilds, jobs_by_parent


def validate_checkout(
    api, merged_data, evaluate_entry, pairs, checkout, graph_cache
):
    """Validate one checkout node; returns a per-checkout report dict"""
    revision = checkout.get("data", {}).get("kernel_revision", {})
    tree = revision.get("tree")
    branch = revision.get("branch")
    commit = revision.get("commit")
    report = {
        "id": checkout.get("id"),
        "tree": tree,
        "branch": branch,
        "commit": commit,
        "state": checkout.get("state"),
        "result": checkout.get("result"),
        "owner": checkout.get("owner"),
        "skipped": None,
        "findings": [],
        "external": [],
        "non_scheduler": [],
        "skipped_failed_kbuilds": [],
        "unsettled_kbuilds": [],
    }

    report["skipped"] = _checkout_skip_reason(merged_data, checkout)
    if report["skipped"]:
        return report

    edges = _checkout_forecast(
        merged_data, evaluate_entry, checkout, graph_cache
    )
    kbuilds, jobs_by_parent = _fetch_children(api, checkout)

    expected_root = edges.get("checkout", set())
    expected_kbuilds = {
        (name, None): runtime
        for name, _, kind, runtime in expected_root
        if kind == "kbuild"
    }
    expected_root_jobs = {
        (name, platform): runtime
        for name, platform, kind, runtime in expected_root
        if kind == "job"
    }

    missing, unexpected = match_children(expected_kbuilds, kbuilds)
    if node_ok(checkout):
        for key in missing:
            name, _ = key
            report["findings"].append(
                {
                    "category": "MISSING_KBUILD",
                    "parent": checkout["id"],
                    "parent_name": "checkout",
                    "job": name,
                    "platform": None,
                    "runtime": expected_kbuilds[key],
                }
            )
    for node in unexpected:
        _report_unexpected(report, pairs, node, "checkout", "UNEXPECTED_KBUILD")

    root_jobs = jobs_by_parent.get(checkout["id"], [])
    _validate_jobs(
        report,
        pairs,
        expected_root_jobs,
        root_jobs,
        source_name="checkout",
        parent_id=checkout["id"],
        parent_ok=node_ok(checkout),
    )

    for kbuild in kbuilds:
        expected_jobs = {
            (name, platform): runtime
            for name, platform, kind, runtime in edges.get(
                kbuild.get("name"), set()
            )
            if kind == "job"
        }
        actual_jobs = jobs_by_parent.get(kbuild.get("id"), [])
        if not node_settled(kbuild):
            report["unsettled_kbuilds"].append(describe_node(kbuild))
            continue
        if not node_ok(kbuild):
            # A failed kbuild legitimately triggers nothing; only count it.
            report["skipped_failed_kbuilds"].append(describe_node(kbuild))
            continue
        _validate_jobs(
            report,
            pairs,
            expected_jobs,
            actual_jobs,
            source_name=kbuild.get("name"),
            parent_id=kbuild.get("id"),
            parent_ok=True,
        )

    return report


def _report_unexpected(report, pairs, node, source_name, category):
    """File an unexpected node as external, non-scheduler or a finding"""
    owner = node.get("owner")
    if owner and report["owner"] and owner != report["owner"]:
        report["external"].append(describe_node(node))
        return
    if (source_name, node.get("name")) not in pairs:
        report["non_scheduler"].append(describe_node(node))
        return
    report["findings"].append(
        {
            "category": category,
            "parent": node.get("parent"),
            "job": node.get("name"),
            "platform": node.get("data", {}).get("platform"),
            "node": describe_node(node),
        }
    )


def _validate_jobs(
    report, pairs, expected, actual, source_name, parent_id, parent_ok
):
    missing, unexpected = match_children(expected, actual)
    if parent_ok:
        for key in missing:
            name, platform = key
            report["findings"].append(
                {
                    "category": "MISSING_JOB",
                    "parent": parent_id,
                    "parent_name": source_name,
                    "job": name,
                    "platform": platform,
                    "runtime": expected[key],
                }
            )
    for node in unexpected:
        _report_unexpected(report, pairs, node, source_name, "UNEXPECTED_JOB")


def annotate(checkout_report):
    """Emit one GitHub Actions annotation per checkout with findings"""
    counts = {}
    for finding in checkout_report["findings"]:
        counts[finding["category"]] = counts.get(finding["category"], 0) + 1
    if not counts:
        return
    level = (
        "warning"
        if any(category in counts for category in MISSING_CATEGORIES)
        else "notice"
    )
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    viewer = f"https://api.kernelci.org/viewer?node_id={checkout_report['id']}"
    print(
        f"::{level}::{checkout_report['tree']}:{checkout_report['branch']} "
        f"checkout {checkout_report['id']}: {summary} ({viewer})"
    )


def print_report(checkout_report, verbose):
    header = (
        f"Checkout {checkout_report['id']} "
        f"{checkout_report['tree']}:{checkout_report['branch']} "
        f"({checkout_report['commit']}) "
        f"state={checkout_report['state']} result={checkout_report['result']}"
    )
    print(header)
    if checkout_report["skipped"]:
        print(f"  skipped: {checkout_report['skipped']}")
        return
    for finding in checkout_report["findings"]:
        platform = finding.get("platform")
        target = finding["job"] + (f" on {platform}" if platform else "")
        detail = finding.get("node") or (
            f"expected from {finding.get('parent_name')} "
            f"via {finding.get('runtime')} (parent {finding['parent']})"
        )
        print(f"  [{finding['category']}] {target}: {detail}")
    for key, label in (
        (
            "skipped_failed_kbuilds",
            "kbuilds skipped (failed, no jobs expected)",
        ),
        ("unsettled_kbuilds", "kbuilds not settled yet, skipped"),
        ("external", "externally-owned nodes (not validated)"),
        ("non_scheduler", "runner-created nodes (not scheduler jobs)"),
    ):
        if checkout_report[key]:
            print(f"  {label}: {len(checkout_report[key])}")
            if verbose:
                for line in checkout_report[key]:
                    print(f"    - {line}")
    if not checkout_report["findings"]:
        print("  OK")


def summarize(reports):
    summary = {
        "checkouts": len(reports),
        "checkouts_skipped": sum(1 for r in reports if r["skipped"]),
        "skipped_failed_kbuilds": sum(
            len(r["skipped_failed_kbuilds"]) for r in reports
        ),
        "unsettled_kbuilds": sum(len(r["unsettled_kbuilds"]) for r in reports),
        "external_nodes": sum(len(r["external"]) for r in reports),
        "non_scheduler_nodes": sum(len(r["non_scheduler"]) for r in reports),
    }
    for category in MISSING_CATEGORIES + UNEXPECTED_CATEGORIES:
        summary[category] = sum(
            1
            for r in reports
            for f in r["findings"]
            if f["category"] == category
        )
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Validate Maestro scheduling against the pipeline forecast"
    )
    parser.add_argument(
        "--api-url",
        default="https://api.kernelci.org",
        help="Maestro API base URL",
    )
    parser.add_argument(
        "--config-dir", default="config", help="Pipeline config directory"
    )
    parser.add_argument(
        "--core-dir",
        help="Path to a kernelci-core tree to import the forecast code from",
    )
    parser.add_argument(
        "--age-hours",
        type=float,
        default=24,
        help="Only validate checkouts at least this old, so the whole "
        "job tree has settled (default: 24)",
    )
    parser.add_argument(
        "--window-hours",
        type=float,
        default=24,
        help="Length of the validated period before --age-hours (default: 24)",
    )
    parser.add_argument("--tree", help="Only validate this tree")
    parser.add_argument("--branch", help="Only validate this branch")
    parser.add_argument(
        "--checkout", help="Only validate this checkout node ID"
    )
    parser.add_argument(
        "--max-checkouts",
        type=int,
        default=0,
        help="Cap the number of checkouts to validate (0 = no cap)",
    )
    parser.add_argument("--json", help="Write the full report to this file")
    parser.add_argument(
        "--fail-on-miss",
        action="store_true",
        help="Exit 1 when forecast jobs are missing from Maestro",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="List skipped failed kbuilds"
    )
    args = parser.parse_args()

    evaluate_entry = import_forecast(args.core_dir)
    merged_data = merge_files(args.config_dir)
    pairs = scheduler_pairs(merged_data)
    api = MaestroAPI(args.api_url)

    if args.checkout:
        resp = api.session.get(
            f"{api.base}/latest/node/{args.checkout}", timeout=api.timeout
        )
        resp.raise_for_status()
        checkouts = [resp.json()]
    else:
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        newest = now - datetime.timedelta(hours=args.age_hours)
        oldest = newest - datetime.timedelta(hours=args.window_hours)
        filters = {
            "kind": "checkout",
            "created__gt": oldest.isoformat(),
            "created__lt": newest.isoformat(),
        }
        if args.tree:
            filters["data.kernel_revision.tree"] = args.tree
        if args.branch:
            filters["data.kernel_revision.branch"] = args.branch
        checkouts = api.nodes(**filters)
        checkouts.sort(key=lambda node: node.get("created") or "")
        print(
            f"Validating {len(checkouts)} checkout(s) created between "
            f"{oldest.isoformat()} and {newest.isoformat()}"
        )

    if args.max_checkouts and len(checkouts) > args.max_checkouts:
        print(
            f"NOTE: only validating the first {args.max_checkouts} of "
            f"{len(checkouts)} checkouts (--max-checkouts)"
        )
        checkouts = checkouts[: args.max_checkouts]

    graph_cache = {}
    reports = []
    for checkout in checkouts:
        report = validate_checkout(
            api, merged_data, evaluate_entry, pairs, checkout, graph_cache
        )
        reports.append(report)
        print_report(report, args.verbose)
        annotate(report)

    summary = summarize(reports)
    print("\n=== Forecast validation summary ===")
    for key, value in summary.items():
        print(f"{key}: {value}")

    if args.json:
        with open(args.json, "w") as fp:
            json.dump({"summary": summary, "checkouts": reports}, fp, indent=2)

    missing = sum(summary[category] for category in MISSING_CATEGORIES)
    if args.fail_on_miss and missing:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
