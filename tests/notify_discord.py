#!/usr/bin/env python3
"""
Post a brief dashboard-validation summary to a Discord webhook.

Reads the reports produced by the validate-dashboard workflow:

  * <name>.json   - `kci-dev maestro validate ... --json` (dashboard checks)
  * forecast.json - tests/check_forecast_coverage.py --json-out
  * integrity.json - tests/check_node_integrity.py --json-out

and posts one embed with a line per check. The webhook URL is taken from
DISCORD_WEBHOOK_URL; without it the message is only printed.
"""

import argparse
import collections
import json
import os
import sys

import requests

COLOR_OK = 0x2ECC71
COLOR_FAIL = 0xE74C3C
# Discord caps an embed description at 4096 characters.
MAX_DESCRIPTION = 4000


def load_report(path):
    try:
        with open(path, "r") as fp:
            text = fp.read().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def flag(ok):
    return "✅" if ok else "❌"


def dashboard_line(name, report):
    if report is None:
        return f"❌ **{name}**: no report (validation step crashed?)", False
    summary = report["summary"]
    line = (
        f"{flag(summary['ok'])} **{name}**: "
        f"{summary['checked'] - summary['failed']}/{summary['checked']} "
        f"commits ok, missing {summary['missing']}, "
        f"status mismatch {summary['status_mismatches']}, "
        f"extra {summary['extra_in_dashboard']}"
    )
    return line, summary["ok"]


def forecast_lines(report, top):
    if report is None:
        return ["❌ **forecast**: no report (forecast step crashed?)"], False
    summary = report["summary"]
    lines = [
        f"{flag(summary['ok'])} **forecast**: "
        f"{summary['checkouts_ok']}/{summary['checkouts']} checkouts complete, "
        f"builds {summary['found']}/{summary['expected']}, "
        f"missing {summary['missing']}, "
        f"incomplete {summary['incomplete']}, "
        f"failed {summary['failed']}, "
        f"checkout failures {summary['checkout_failures']}"
    ]

    # Name the worst offenders so the message is actionable on its own.
    missing = collections.Counter()
    incomplete = collections.Counter()
    for entry in report["checkouts"]:
        missing.update(entry["missing"])
        incomplete.update(
            f"{entry['tree_branch']} ({item['reason']})"
            for item in entry["incomplete"]
        )
    checkout_failures = collections.Counter(
        f"{item['tree_branch']} ({item['reason']})"
        for item in report["checkout_failures"]
    )
    for title, counter in (
        ("missing", missing),
        ("incomplete", incomplete),
        ("checkout failures", checkout_failures),
    ):
        if counter:
            items = ", ".join(
                f"{key} ×{count}" for key, count in counter.most_common(top)
            )
            more = len(counter) - top
            if more > 0:
                items += f", +{more} more"
            lines.append(f"  • {title}: {items}")
    return lines, summary["ok"]


def integrity_lines(report, top):
    if report is None:
        return ["❌ **done nodes**: no report (integrity step crashed?)"], False
    summary = report["summary"]
    lines = [
        f"{flag(summary['ok'])} **done nodes**: "
        f"{summary['rejected']} rejected changes, "
        f"{summary['forced']} forced by admin"
    ]
    # These are rare and each may hide a wrong result, so link every one.
    for event in report["events"][:top]:
        viewer = f"https://api.kernelci.org/viewer?node_id={event['id']}"
        lines.append(
            f"  • [{event['name']}]({viewer}) "
            f"{event.get('platform') or ''} ({event.get('runtime')}): "
            f"{event['result']} → {event['attempted_result']}"
        )
    more = len(report["events"]) - top
    if more > 0:
        lines.append(f"  • +{more} more")
    return lines, summary["ok"]


def main():
    parser = argparse.ArgumentParser(
        description="Post dashboard validation summary to Discord"
    )
    parser.add_argument(
        "--reports-dir", required=True, help="Directory with the JSON reports"
    )
    parser.add_argument(
        "--checks",
        nargs="+",
        default=["builds", "boots", "builds-history"],
        help="Dashboard check names (report file basenames)",
    )
    parser.add_argument(
        "--top", type=int, default=5, help="Offenders to list per category"
    )
    parser.add_argument("--run-url", help="Link to the workflow run")
    args = parser.parse_args()

    lines = []
    all_ok = True
    for name in args.checks:
        report = load_report(os.path.join(args.reports_dir, f"{name}.json"))
        line, ok = dashboard_line(name, report)
        lines.append(line)
        all_ok = all_ok and ok
    report = load_report(os.path.join(args.reports_dir, "forecast.json"))
    flines, ok = forecast_lines(report, args.top)
    lines.extend(flines)
    all_ok = all_ok and ok
    report = load_report(os.path.join(args.reports_dir, "integrity.json"))
    ilines, ok = integrity_lines(report, args.top)
    lines.extend(ilines)
    all_ok = all_ok and ok

    description = "\n".join(lines)
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION] + "…"
    embed = {
        "title": f"{flag(all_ok)} KernelCI dashboard validation",
        "description": description,
        "color": COLOR_OK if all_ok else COLOR_FAIL,
    }
    if args.run_url:
        embed["url"] = args.run_url
    print(f"{embed['title']}\n{description}")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        print("DISCORD_WEBHOOK_URL is not set; not posting.")
        return 0
    res = requests.post(webhook, json={"embeds": [embed]}, timeout=30)
    if res.status_code >= 300:
        print(f"Discord webhook failed: HTTP {res.status_code} {res.text}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
