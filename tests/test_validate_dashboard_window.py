"""Regression coverage for the dashboard's result publication window."""

import json
import sys
from unittest.mock import patch

import validate_dashboard_window as validation

START = validation.utc("2026-11-17T13:57:03Z")
END = validation.utc("2026-11-18T13:57:03Z")
REV = ("stable", "https://example.org/linux.git", "main", "abc")


def test_compare_uses_same_half_open_window_on_both_sides():
    def build(node_id, updated):
        return {
            "id": node_id,
            "updated": updated,
            "result": "pass",
            "retry_counter": 0,
        }

    def dashboard(node_id, started, status="PASS"):
        return {
            "id": f"maestro:{node_id}",
            "start_time": started,
            "status": status,
        }

    older = "2026-11-17T12:00:00Z"
    source = [
        build("missing", START.isoformat()),  # inclusive
        build("wrong-status", START.isoformat()),
        build("late-publication", END.isoformat()),  # exclusive
        build("already-published", END.isoformat()),
        build("old", older),
    ]
    rows = [
        dashboard("already-published", START.isoformat()),
        dashboard("wrong-status", older, "FAIL"),
        dashboard("old", older),
        dashboard("extra", START.isoformat()),
        dashboard("future-extra", END.isoformat()),
    ]
    with (
        patch.object(validation, "nodes", return_value=source),
        patch.object(
            validation, "dashboard_api_fetch", return_value={"builds": rows}
        ),
    ):
        stats = validation.compare("api", REV, [], "build", START, END)
        following = validation.compare(
            "api", REV, [], "build", END, END + (END - START)
        )
    assert stats[2:] == [2, 2, "❌", ["missing"], ["wrong-status"], ["extra"]]
    assert following[5:] == [["late-publication"], [], ["future-extra"]]


def test_history_discovers_late_result_after_checkout_ages_out():
    def node(commit):
        return {
            "data": {
                "kernel_revision": {
                    "tree": REV[0],
                    "url": REV[1],
                    "branch": REV[2],
                    "commit": commit,
                }
            }
        }

    checkout = dict(node("current"), created=START.isoformat())
    with (
        patch.object(validation, "dashboard_heads", return_value=set()),
        patch.object(
            validation, "nodes", side_effect=[[checkout], [node("old")]]
        ) as fetch,
    ):
        selected = validation.revisions(
            "api", ["kind=kbuild"], START, END, "builds-history"
        )
    assert selected == [REV[:3] + ("current",), REV[:3] + ("old",)]
    filters = fetch.call_args_list[1].args[1]
    assert f"updated__gte={START.isoformat()}" in filters
    assert f"updated__lt={END.isoformat()}" in filters


def test_workflow_window_and_summary(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "kci-dev.toml"
    settings.write_text(
        'default_instance="test"\n[test]\napi="https://example.org"\n'
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_dashboard_window.py",
            "builds",
            "--as-of",
            "2026-11-18T14:57:03Z",
            "--settings",
            str(settings),
            "--summary-file",
            str(summary),
        ],
    )
    with patch.object(validation, "revisions", return_value=[]) as select:
        assert validation.main() == 0
    assert select.call_args.args[2:4] == (START, END)
    report = json.loads(capsys.readouterr().out)
    assert report["window"] == {
        "since": START.isoformat(),
        "until": END.isoformat(),
    }
    assert END.isoformat() in summary.read_text()
