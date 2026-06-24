#!/usr/bin/env python3
"""
Explain dashboard-validation misses by cross-referencing pipeline config.

`kci-dev maestro validate boots/builds --json` reports node IDs that are in
Maestro but missing from (or mismatched on) the dashboard/KCIDB. It cannot say
*why*, because it has no knowledge of the pipeline job config. This script
parses that JSON output (kci-dev is left untouched) and classifies each flagged
node against the merged pipeline config:

  * UNMAPPED_EXTERNAL - the node's job is not defined in our config at all.
    These are externally-owned jobs (e.g. owner=qualcomm submits via Maestro and
    runs its own KCIDB bridge). Our bridge can't map the test path, so the node
    is sent under its raw job name instead of `boot`. Expected, informational.
  * MISSING_SUITE - the job IS in our config but has no kcidb_test_suite, so the
    bridge drops it ("path information is missing"). This is a config bug we own;
    validate_yaml.py now rejects it statically, so it should not occur, but we
    flag it loudly if it slips through.
  * DEFINED_OK - job defined and mapped; the miss has some other cause worth a
    closer look.
  * UNKNOWN_NODE - the node could not be fetched.

Exit status is 0 unless a MISSING_SUITE node is found (a mapping regression we
own); count/mismatch failures are already surfaced by kci-dev itself.
"""

import argparse
import json
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_yaml import merge_files  # noqa: E402

# kinds the KCIDB bridge expects a kcidb_test_suite mapping for
MAPPED_KINDS = ("test", "job")


def collect_flagged_ids(reports):
    """Return {node_id: reason} from one or more kci-dev JSON reports."""
    flagged = {}
    for report in reports:
        for result in report.get("results", []):
            for node_id in result.get("missing_ids", []):
                flagged.setdefault(node_id, "missing")
            for node_id in result.get("status_mismatch_ids", []):
                flagged.setdefault(node_id, "status_mismatch")
    return flagged


def load_reports(paths):
    """Load kci-dev JSON reports; skip empty files (e.g. all-pass runs)."""
    reports = []
    for path in paths:
        with open(path, "r") as fp:
            text = fp.read().strip()
        if not text:
            continue
        reports.append(json.loads(text))
    return reports


def fetch_node(api_url, node_id, timeout=30):
    url = f"{api_url.rstrip('/')}/latest/node/{node_id}"
    try:
        res = requests.get(url, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return None, str(exc)
    if res.status_code != 200:
        return None, f"HTTP {res.status_code}"
    return res.json(), None


def classify_node(node, jobs):
    """Return (category, detail) for a fetched node."""
    name = node.get("name")
    owner = node.get("owner")
    runtime = node.get("data", {}).get("runtime")
    job = jobs.get(name)
    if job is None:
        return "UNMAPPED_EXTERNAL", (
            f"job '{name}' not found in pipeline config "
            f"(owner={owner}, runtime={runtime})"
        )
    if job.get("kind") in MAPPED_KINDS and not job.get("kcidb_test_suite"):
        return "MISSING_SUITE", (
            f"job '{name}' is in config but has no kcidb_test_suite mapping"
        )
    return "DEFINED_OK", (
        f"job '{name}' is defined and mapped "
        f"(kcidb_test_suite={job.get('kcidb_test_suite')})"
    )


def annotate(category, node_id, detail):
    """Emit a GitHub Actions annotation plus a plain line."""
    level = "warning" if category == "MISSING_SUITE" else "notice"
    viewer = f"https://api.kernelci.org/viewer?node_id={node_id}"
    print(f"::{level}::[{category}] {node_id}: {detail} ({viewer})")


def main():
    parser = argparse.ArgumentParser(
        description="Classify dashboard-validation misses against pipeline config"
    )
    parser.add_argument(
        "reports", nargs="+", help="kci-dev validate --json report file(s)"
    )
    parser.add_argument(
        "--config-dir", default="config", help="Pipeline config directory"
    )
    parser.add_argument(
        "--api-url",
        default="https://api.kernelci.org",
        help="Maestro API base URL",
    )
    parser.add_argument(
        "--max-nodes",
        type=int,
        default=300,
        help="Maximum number of flagged nodes to inspect",
    )
    args = parser.parse_args()

    reports = load_reports(args.reports)
    flagged = collect_flagged_ids(reports)
    if not flagged:
        print("No missing or mismatched nodes to classify.")
        return 0

    merged = merge_files(args.config_dir)
    jobs = merged.get("jobs", {})

    node_ids = list(flagged)
    truncated = 0
    if len(node_ids) > args.max_nodes:
        truncated = len(node_ids) - args.max_nodes
        node_ids = node_ids[: args.max_nodes]

    buckets = {}
    for node_id in node_ids:
        node, err = fetch_node(args.api_url, node_id)
        if node is None:
            category, detail = "UNKNOWN_NODE", f"could not fetch node: {err}"
        else:
            category, detail = classify_node(node, jobs)
        buckets.setdefault(category, []).append((node_id, detail))
        annotate(category, node_id, detail)

    print("\n=== Dashboard miss classification ===")
    for category in (
        "UNMAPPED_EXTERNAL",
        "MISSING_SUITE",
        "DEFINED_OK",
        "UNKNOWN_NODE",
    ):
        items = buckets.get(category, [])
        if items:
            print(f"{category}: {len(items)}")
    if truncated:
        print(
            f"NOTE: {truncated} additional flagged node(s) not inspected "
            f"(--max-nodes={args.max_nodes})"
        )

    # Only a config mapping we own (MISSING_SUITE) is an actionable regression
    # here; external/unmapped jobs and count deltas are reported elsewhere.
    return 1 if buckets.get("MISSING_SUITE") else 0


if __name__ == "__main__":
    sys.exit(main())
