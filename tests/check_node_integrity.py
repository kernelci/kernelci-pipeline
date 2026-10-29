#!/usr/bin/env python3
"""
Report attempts to change nodes that were already done.

kernelci-api rejects (HTTP 409) any update that changes the state or result of
a done node, e.g. a lab reporting pass and then fail for the same job, and
publishes each attempt on the `node-integrity` channel. Admin overrides
(force=true) are accepted but published there as well. Such nodes may hold the
wrong result in Maestro and on the dashboard, so they need a manual look.

This script lists those events for the last N days. Exit status is 1 if any
attempt was found.
"""

import argparse
import collections
import datetime
import json
import sys

import requests

CHANNEL = "node-integrity"


def fetch_events(api_url, since, limit, timeout=60):
    url = f"{api_url.rstrip('/')}/latest/events"
    params = {
        "channel": CHANNEL,
        "from": since.isoformat(timespec="seconds"),
        "limit": limit,
    }
    res = requests.get(url, params=params, timeout=timeout)
    res.raise_for_status()
    return [event["data"] for event in res.json()]


def main():
    parser = argparse.ArgumentParser(
        description="Report rejected/forced changes to done nodes"
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
        help="Report events from the last N days",
    )
    parser.add_argument(
        "--limit", type=int, default=1000, help="Maximum events to fetch"
    )
    parser.add_argument("--json-out", help="Write the report to this file")
    args = parser.parse_args()

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    since = now - datetime.timedelta(days=args.days)
    events = fetch_events(args.api_url, since, args.limit)

    for event in events:
        print(
            f"::warning::[{event['op']}] {event['id']} {event['name']} "
            f"({event.get('runtime')}/{event.get('platform')}): "
            f"{event['state']}/{event['result']} -> "
            f"{event['attempted_state']}/{event['attempted_result']} "
            f"by {event['user']} "
            f"(https://api.kernelci.org/viewer?node_id={event['id']})"
        )

    ops = collections.Counter(event["op"] for event in events)
    report = {
        "window": {"since": since.isoformat(timespec="seconds")},
        "summary": {
            "events": len(events),
            "rejected": ops.get("done_update_rejected", 0),
            "forced": ops.get("done_update_forced", 0),
            "ok": not events,
        },
        "events": events,
    }

    print("\n=== Done node integrity ===")
    for key, value in report["summary"].items():
        print(f"{key}: {value}")

    if args.json_out:
        with open(args.json_out, "w") as fp:
            json.dump(report, fp, indent=2)

    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
