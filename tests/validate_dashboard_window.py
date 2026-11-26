#!/usr/bin/env python3
"""Compare Maestro and dashboard results after allowing time for publication."""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import click
import tomllib
from kcidev.libs.dashboard import configure_dashboard_api, dashboard_api_fetch
from kcidev.libs.maestro_common import maestro_get_nodes
from kcidev.subcommands.maestro.validate import helper


def utc(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def nodes(api, filters):
    offset = 0
    while True:
        page = maestro_get_nodes(api, 250, offset, filters, paginate=True)
        yield from page
        if len(page) < 250:
            return
        offset += len(page)


def dashboard_heads(since, until):
    # The dashboard tree listing accepts whole days counted back from today.
    # Narrow it to the exact window locally, including on historical replays.
    days = max(1, (datetime.now(timezone.utc) - since).days + 1)
    try:
        rows = dashboard_api_fetch(
            "tree",
            {"origin": "maestro", "interval_in_days": days},
            False,
            error_verbose=False,
        )
    except click.ClickException as exc:
        if "No trees were found" in str(exc):
            return set()
        raise
    return {
        (
            r["tree_name"],
            r["git_repository_url"],
            r["git_repository_branch"],
            r["git_commit_hash"],
        )
        for r in rows
        if since <= utc(r["start_time"]) < until
    }


def revisions(api, filters, since, until, check):
    """Select heads in the checkout window and discover late work on older commits."""

    def select_window(field):
        return [
            f"{field}__gte={since.isoformat()}",
            f"{field}__lt={until.isoformat()}",
        ]

    def revision(node):
        rev = node["data"]["kernel_revision"]
        return rev["tree"], rev["url"], rev["branch"], rev["commit"]

    heads = {}
    # Keep dashboard-only heads in scope so extra IDs can still be detected.
    selected = dashboard_heads(since, until)
    for node in nodes(
        api, ["kind=checkout", "state=done", *select_window("created")]
    ):
        rev = revision(node)
        head = (node["created"], rev)
        heads[rev[:3]] = max(head, heads.get(rev[:3], head))
        if check == "builds-history":
            selected.add(rev)
    selected.update(rev for _, rev in heads.values())

    # The history check covers builds on superseded heads. Boots have no
    # separate history check, so include every commit with boot activity.
    if check != "builds":
        for node in nodes(api, [*filters, *select_window("updated")]):
            selected.add(revision(node))
    return sorted(selected)


def boot_results(source, boot_jobs):
    """The KCIDB bridge publishes each eligible root and its result leaves."""
    children = defaultdict(list)
    for node in source:
        children[node["parent"]].append(node)

    node_ids = {node["id"] for node in source}
    roots = [
        node
        for node in source
        if node["parent"] not in node_ids and node["name"] in boot_jobs
    ]
    for root in helper.filter_maestro_items_for_validation(roots, "boot"):
        yield root
        pending = list(children.get(root["id"], ()))
        while pending:
            node = pending.pop()
            if node["id"] in children:
                pending.extend(children[node["id"]])
                continue
            # send_kcidb omits setup results except for os-release.
            path = ".".join(node["path"][len(root["path"]) :])
            if "setup" not in path or "os-release" in path:
                yield node


def boot_status_mismatches(boots, dashboard):
    # kci-dev handles incomplete boot error codes, but its completed-result
    # map only covers PASS and FAIL. Individual tests can also report SKIP.
    incomplete = [node for node in boots if node["result"] == "incomplete"]
    mismatches = helper.validate_boot_status(incomplete, dashboard)
    statuses = {row["id"].split(":", 1)[1]: row["status"] for row in dashboard}
    for node in boots:
        if node["id"] in statuses and node["result"] != "incomplete":
            expected = node["result"] and node["result"].upper()
            if statuses[node["id"]] != expected:
                mismatches.append(node["id"])
    return mismatches


def compare(api, rev, filters, kind, since, until, boot_jobs=()):
    tree, url, branch, commit = rev
    source = list(
        nodes(
            api,
            [
                *filters,
                f"data.kernel_revision.url={url}",
                f"data.kernel_revision.branch={branch}",
                f"data.kernel_revision.commit={commit}",
            ],
        )
    )
    timestamps = {n["id"]: utc(n["updated"]) for n in source}
    if kind == "boot":
        source = boot_results(source, boot_jobs)
    else:
        source = helper.filter_maestro_items_for_validation(source, kind)
    eligible = [n for n in source if since <= timestamps[n["id"]] < until]

    params = {"origin": "maestro", "git_url": url, "git_branch": branch}
    if kind == "boot":
        params["filter_boot.origin"] = "maestro"
    plural = f"{kind}s"
    try:
        rows = dashboard_api_fetch(
            f"tree/{commit}/{plural}", params, False, error_verbose=False
        )[plural]
    except click.ClickException as exc:
        if not any(
            msg in str(exc)
            for msg in (
                f"No {plural} available",
                "No results available for this tree/branch/commit",
                "Tree checkout not found",
            )
        ):
            raise
        rows = []

    # Apply the same Maestro timestamp on both sides: a recently updated
    # result can already be visible. For a dashboard-only ID use its own time.
    dashboard = []
    for row in rows:
        stamp = timestamps.get(row["id"].split(":", 1)[1])
        if stamp is None and row.get("start_time"):
            stamp = utc(row["start_time"])
        if stamp is None or since <= stamp < until:
            dashboard.append(row)

    # Eligibility has already been applied to the roots. A result leaf
    # belongs to its boot attempt and has no independent retry policy.
    maestro_ids = {node["id"] for node in eligible}
    dashboard_ids = {row["id"].split(":", 1)[1] for row in dashboard}
    missing = sorted(maestro_ids - dashboard_ids)
    extra = sorted(dashboard_ids - maestro_ids)
    check_status = (
        boot_status_mismatches
        if kind == "boot"
        else helper.validate_build_status
    )
    mismatch = check_status(eligible, dashboard)
    ok = len(eligible) == len(dashboard) and not (missing or extra or mismatch)
    return [
        f"{tree}/{branch}",
        commit,
        len(eligible),
        len(dashboard),
        "✅" if ok else "❌",
        missing,
        mismatch,
        extra,
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("check", choices=("builds", "boots", "builds-history"))
    parser.add_argument(
        "--as-of", required=True, type=utc, help="Run timestamp (UTC)"
    )
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--settings", default="tools/cron/kci-dev.toml")
    parser.add_argument(
        "--summary-file", help="Append a GitHub Actions step summary"
    )
    parser.add_argument("--pipeline-config", default="config")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be positive")
    until = args.as_of - timedelta(hours=1)
    since = until - timedelta(days=args.days)
    print(
        f"Dashboard window [{since.isoformat()}, {until.isoformat()})",
        file=sys.stderr,
    )

    with open(args.settings, "rb") as stream:
        settings = tomllib.load(stream)
    instance = settings["default_instance"]
    api = settings[instance]["api"]
    configure_dashboard_api(settings, instance)
    history = args.check == "builds-history"
    kind = "boot" if args.check == "boots" else "build"
    boot_jobs = ()
    if kind == "boot":
        boot_jobs = helper.load_boot_job_names(args.pipeline_config)
        filters = ["state=done", f"path__in={','.join(boot_jobs)}"]
    else:
        filters = ["kind=kbuild", "state__ne=running"]
        if history:
            filters.append("data.error_code__ne=node_timeout")
    rows = [
        compare(api, rev, filters, kind, since, until, boot_jobs)
        for rev in revisions(api, filters, since, until, args.check)
    ]
    report = helper.validation_rows_to_json(
        f"{kind}s", rows, history, "maestro", args.days, None
    )
    report["window"] = {"since": since.isoformat(), "until": until.isoformat()}
    helper.print_validation_human(report, err=True)
    if args.summary_file:
        summary = report["summary"]
        with open(args.summary_file, "a", encoding="utf-8") as stream:
            print(
                f"Dashboard window from {since.isoformat()} to {until.isoformat()}\n",
                file=stream,
            )
            print(
                f"{summary['checked']} commits checked, {summary['missing']} missing, "
                f"{summary['status_mismatches']} status mismatches, "
                f"{summary['extra_in_dashboard']} extra",
                file=stream,
            )
    print(json.dumps(report))
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
