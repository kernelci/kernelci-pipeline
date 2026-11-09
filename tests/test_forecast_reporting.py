"""Forecast notifications identify execution locations without changing status."""

from unittest.mock import Mock

import pytest

import check_forecast_coverage as coverage
from notify_discord import forecast_lines


@pytest.mark.parametrize(
    "state,result,reason",
    [
        ("running", None, "state=running"),
        ("done", "incomplete", "node_timeout"),
        ("done", "fail", None),
    ],
)
def test_report_keeps_location_of_latest_attempt(
    monkeypatch, state, result, reason
):
    old = {
        "id": "old",
        "name": "baseline",
        "kind": "job",
        "created": "2026-11-07T00:00:00",
        "state": "done",
        "result": "fail",
        "data": {"runtime": "lava-old"},
    }
    latest = dict(
        old,
        id="latest",
        created="2026-11-08T00:00:00",
        state=state,
        result=result,
        data={
            "runtime": "lava-collabora",
            "platform": "imx6q-sabrelite",
            "error_code": "node_timeout" if reason else None,
        },
    )
    monkeypatch.setattr(coverage, "fetch_nodes", lambda *args: [latest, old])
    checkout = {
        "id": "checkout",
        "result": "pass",
        "data": {"kernel_revision": {"tree": "mainline", "branch": "master"}},
    }
    report = coverage.build_report(
        "unused", Mock(expected=Mock(return_value={"baseline"})), [checkout]
    )
    category = "incomplete" if reason else "failed"
    (item,) = report["checkouts"][0][category]
    assert item["id"] == "latest"
    assert item["runtime"] == "lava-collabora"
    assert item["platform"] == "imx6q-sabrelite"
    if reason:
        assert item["reason"] == reason
    lines, ok = forecast_lines(report, top=5)
    assert ok == (result == "fail")
    assert any("lab=lava-collabora; device=imx6q-sabrelite" in x for x in lines)
    assert not any("lava-old" in x for x in lines)


def test_incomplete_notifications_separate_labs_and_build_clusters():
    locations = [
        {"kind": "job", "runtime": "lava-collabora"},
        {"kind": "test", "runtime": "lava-broonie"},
        {"kind": "kbuild", "runtime": "k8s-all", "job_context": "aks-build"},
    ]
    report = {
        "summary": {
            "ok": False,
            "checkouts_ok": 0,
            "checkouts": 1,
            "found": 3,
            "expected": 3,
            "missing": 0,
            "incomplete": 3,
            "failed": 0,
            "checkout_failures": 0,
        },
        "checkouts": [
            {
                "tree_branch": "mainline/master",
                "missing": [],
                "failed": [],
                "incomplete": [
                    dict(x, reason="node_timeout") for x in locations
                ],
            }
        ],
        "checkout_failures": [],
    }
    lines, ok = forecast_lines(report, top=5)
    assert not ok
    details = lines[1]
    assert "lab=lava-collabora) ×1" in details
    assert "lab=lava-broonie) ×1" in details
    assert "runtime=k8s-all; cluster=aks-build) ×1" in details
    assert "lab=k8s-all" not in details

    # Reports uploaded before location metadata was added still render.
    report["checkouts"][0]["incomplete"] = [{"reason": "node_timeout"}]
    lines, _ = forecast_lines(report, top=5)
    assert lines[1] == "  • incomplete: mainline/master (node_timeout) ×1"


def test_build_report_keeps_kubernetes_cluster(monkeypatch):
    node = {
        "id": "build",
        "name": "kbuild-gcc-14-arm64",
        "kind": "kbuild",
        "state": "done",
        "result": "incomplete",
        "data": {
            "error_code": "node_timeout",
            "runtime": "k8s-all",
            "platform": "kubernetes",
            "job_context": "aks-build",
        },
    }
    monkeypatch.setattr(coverage, "fetch_nodes", lambda *args: [node])
    report = coverage.check_checkout(
        "unused", {"id": "checkout"}, {node["name"]}
    )
    (item,) = report["incomplete"]
    assert item["job_context"] == "aks-build"


def test_report_accepts_nodes_without_location(monkeypatch):
    node = {
        "id": "build",
        "name": "kbuild-gcc-14-arm64",
        "state": "done",
        "result": "fail",
    }
    monkeypatch.setattr(coverage, "fetch_nodes", lambda *args: [node])
    report = coverage.check_checkout(
        "unused", {"id": "checkout"}, {node["name"]}
    )
    assert report["failed"] == [{"name": node["name"], "id": "build"}]
