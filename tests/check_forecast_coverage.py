#!/usr/bin/env python3
"""
Check that recent checkouts produced every build the config forecasts.

`kci config forecast` (kernelci-core) computes, from the pipeline config, which
checkout-triggered jobs (mostly kbuilds) should run for each tree/branch. This
script takes the recent checkouts from the Maestro API, evaluates that forecast
for each one (using the checkout's real kernel version so min/max version rules
are honoured), and compares it with the child nodes the checkout actually got:

  * MISSING     - forecast job with no node under the checkout.
  * INCOMPLETE  - node exists but is not done (stuck) or result=incomplete.
  * FAILED      - node done with result=fail (real build failure, informational).
  * UNEXPECTED  - node exists but was not forecast (informational: forecast
                  drift or a job scheduled from outside this config).

Checkouts that themselves did not complete (e.g. node_timeout) are reported
separately, as no builds are expected to be scheduled for them.

Exit status is 1 if any MISSING or INCOMPLETE build is found.
"""

import argparse
import datetime
import json
import sys

import kernelci.cli.config as kci_forecast
import kernelci.config
import requests

PAGE_LIMIT = 500


def fetch_nodes(api_url, params, timeout=60):
    """Return all nodes matching params, following pagination."""
    url = f"{api_url.rstrip('/')}/latest/nodes"
    items = []
    offset = 0
    while True:
        query = dict(params, limit=PAGE_LIMIT, offset=offset)
        res = requests.get(url, params=query, timeout=timeout)
        res.raise_for_status()
        page = res.json()
        items.extend(page.get("items", []))
        offset += PAGE_LIMIT
        if offset >= page.get("total", 0):
            return items


class Forecast:
    """Expected checkout children per tree/branch, cached per kernel version."""

    def __init__(self, config_dir):
        self._data = kernelci.config.load_yaml(
            kernelci.config.get_config_paths(config_dir)
        )
        self._cache = {}

    def _for_version(self, version):
        key = (version.get("version"), version.get("patchlevel"))
        if key not in self._cache:
            kci_forecast.FORECAST_KERNEL_VERSION = {
                "version": key[0],
                "patchlevel": key[1],
                "extra": version.get("extra", ""),
            }
            expected = {}
            for checkout in kci_forecast.get_forecast_data(self._data):
                names = {kbuild["name"] for kbuild in checkout["kbuilds"]}
                tb = (checkout.get("tree"), checkout.get("branch"))
                expected.setdefault(tb, set()).update(names)
            self._cache[key] = expected
        return self._cache[key]

    def expected(self, tree, branch, version):
        """Return the set of forecast job names, or None if tree is unknown."""
        expected = self._for_version(version)
        if (tree, branch) in expected:
            return expected[(tree, branch)]
        # Trees with dynamically resolved branches (e.g. netdev-testing uses a
        # URL in build_configs) can only be matched on the tree name.
        by_tree = [names for (t, _), names in expected.items() if t == tree]
        if len(by_tree) == 1:
            return by_tree[0]
        return None


def check_checkout(api_url, checkout, expected):
    """Return a dict describing coverage of one checkout."""
    children = fetch_nodes(api_url, {"parent": checkout["id"]})
    by_name = {}
    for child in children:
        by_name.setdefault(child["name"], []).append(child)

    missing = sorted(expected - set(by_name))
    incomplete = []
    failed = []
    for name in sorted(expected & set(by_name)):
        # Retries create several nodes for the same job; the latest wins.
        node = max(by_name[name], key=lambda n: n.get("created", ""))
        if node["state"] != "done":
            incomplete.append(
                {
                    "name": name,
                    "id": node["id"],
                    "reason": f"state={node['state']}",
                }
            )
        elif node.get("result") == "incomplete":
            reason = node.get("data", {}).get("error_code") or "incomplete"
            incomplete.append(
                {"name": name, "id": node["id"], "reason": reason}
            )
        elif node.get("result") == "fail":
            failed.append({"name": name, "id": node["id"]})

    return {
        "expected": len(expected),
        "found": len(expected & set(by_name)),
        "missing": missing,
        "incomplete": incomplete,
        "failed": failed,
        "unexpected": sorted(set(by_name) - expected),
    }


def build_report(api_url, forecast, checkouts):
    """Check every checkout against its forecast and summarise."""
    report = {"checkouts": [], "checkout_failures": [], "unknown_trees": []}
    for checkout in checkouts:
        rev = checkout.get("data", {}).get("kernel_revision", {})
        tree, branch = rev.get("tree"), rev.get("branch")
        label = f"{tree}/{branch}"
        if checkout.get("result") != "pass":
            report["checkout_failures"].append(
                {
                    "tree_branch": label,
                    "id": checkout["id"],
                    "reason": (
                        checkout.get("data", {}).get("error_code")
                        or f"state={checkout['state']}, "
                        f"result={checkout.get('result')}"
                    ),
                }
            )
            continue
        expected = forecast.expected(tree, branch, rev.get("version", {}))
        if expected is None:
            report["unknown_trees"].append(label)
            continue
        entry = check_checkout(api_url, checkout, expected)
        entry.update(
            {
                "tree_branch": label,
                "id": checkout["id"],
                "commit": rev.get("commit"),
            }
        )
        report["checkouts"].append(entry)

    entries = report["checkouts"]
    summary = {
        "checkouts": len(entries),
        "checkouts_ok": sum(
            not e["missing"] and not e["incomplete"] for e in entries
        ),
        "expected": sum(e["expected"] for e in entries),
        "found": sum(e["found"] for e in entries),
        "missing": sum(len(e["missing"]) for e in entries),
        "incomplete": sum(len(e["incomplete"]) for e in entries),
        "failed": sum(len(e["failed"]) for e in entries),
        "unexpected": sum(len(e["unexpected"]) for e in entries),
        "checkout_failures": len(report["checkout_failures"]),
    }
    summary["ok"] = not summary["missing"] and not summary["incomplete"]
    report["summary"] = summary
    return report


def print_report(report):
    """Emit GitHub Actions annotations and a plain-text summary."""
    for entry in report["checkouts"]:
        for name in entry["missing"]:
            print(
                f"::warning::[MISSING] {entry['tree_branch']} "
                f"{entry['id']}: {name} was forecast but never created"
            )
        for item in entry["incomplete"]:
            print(
                f"::warning::[INCOMPLETE] {entry['tree_branch']} "
                f"{item['id']}: {item['name']} ({item['reason']})"
            )
    for item in report["checkout_failures"]:
        print(
            f"::notice::[CHECKOUT] {item['tree_branch']} {item['id']}: "
            f"{item['reason']}"
        )

    print("\n=== Forecast coverage ===")
    print(
        f"Window: {report['window']['since']} .. "
        f"{report['window']['until']} (UTC)"
    )
    for key, value in report["summary"].items():
        print(f"{key}: {value}")
    if report["unknown_trees"]:
        print(
            "Not in forecast: "
            + ", ".join(sorted(set(report["unknown_trees"])))
        )


def main():
    parser = argparse.ArgumentParser(
        description="Compare recent checkouts against the config forecast"
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
        "--days",
        type=float,
        default=1,
        help="Check checkouts created in the last N days",
    )
    parser.add_argument(
        "--grace-hours",
        type=float,
        default=6,
        help="Skip checkouts younger than this, their builds may still run",
    )
    parser.add_argument(
        "--owner",
        default="production",
        help="Only check checkouts with this owner",
    )
    parser.add_argument("--json-out", help="Write the report to this file")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    since = now - datetime.timedelta(days=args.days, hours=args.grace_hours)
    until = now - datetime.timedelta(hours=args.grace_hours)

    forecast = Forecast(args.config_dir)
    checkouts = fetch_nodes(
        args.api_url,
        {
            "kind": "checkout",
            "owner": args.owner,
            "created__gt": since.isoformat(timespec="seconds"),
            "created__lt": until.isoformat(timespec="seconds"),
        },
    )
    checkouts.sort(key=lambda n: n.get("created", ""))

    report = build_report(args.api_url, forecast, checkouts)
    report["window"] = {
        "since": since.isoformat(timespec="seconds"),
        "until": until.isoformat(timespec="seconds"),
    }
    print_report(report)

    if args.json_out:
        with open(args.json_out, "w") as fp:
            json.dump(report, fp, indent=2)

    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
