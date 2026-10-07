# SPDX-License-Identifier: LGPL-2.1-or-later

"""Regression coverage for callbacks from jobs canceled before execution."""

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kernelci.runtime.lava import Callback


@pytest.fixture(scope="module")
def callback_module():
    path = Path(__file__).parents[1] / "src" / "lava_callback.py"
    spec = importlib.util.spec_from_file_location("callback_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with (
        patch("toml.load", return_value={}),
        patch("kernelci.config.load", return_value={}),
        patch("kernelci.config.load_yaml", return_value={}),
        patch("logging.basicConfig"),
    ):
        spec.loader.exec_module(module)
    yield module
    module.executor.shutdown(wait=True)


@pytest.fixture
def canceled_job(callback_module, monkeypatch):
    helper = MagicMock()
    node = {
        "id": "6ac3af1ba3f195e1aa3903e0",
        "name": "rt-tests-rtla-timerlat",
        "state": "running",
        "result": None,
        "data": {"runtime": "lava-collabora", "job_id": "23128513"},
        "artifacts": {},
    }
    helper.api.node.get.return_value = node
    callback = Callback(
        {
            "status": Callback.CANCELED,
            "results": {},
            "definition": "metadata:\n  storage_config_name: test\n",
        }
    )
    emitter = MagicMock()
    monkeypatch.setattr(callback_module, "_get_storage", lambda name: None)
    monkeypatch.setattr(
        callback_module, "_get_telemetry_emitter", lambda: emitter
    )
    monkeypatch.setattr(callback_module, "metrics", MagicMock())
    monkeypatch.setattr(callback_module, "logger", MagicMock())
    return helper, node, callback, emitter


def test_canceled_callback_without_lava_results(callback_module, canceled_job):
    helper, node, callback, emitter = canceled_job

    callback_module.async_job_submit(helper, node["id"], callback)

    helper.submit_results.assert_called_once()
    hierarchy, persisted_node = helper.submit_results.call_args.args
    assert persisted_node["state"] == "done"
    assert persisted_node["result"] == "incomplete"
    assert persisted_node["data"]["error_code"] == "Infrastructure"
    assert hierarchy["child_nodes"] == []
    emitter.emit.assert_called_once()
    assert emitter.emit.call_args.args == ("job_result",)
    assert emitter.emit.call_args.kwargs["is_infra_error"] is True
    assert emitter.emit.call_args.kwargs["result"] == "incomplete"
    callback_module.metrics.add.assert_not_called()
    callback_module.logger.exception.assert_not_called()


@pytest.mark.parametrize(
    "result,error_code,expected",
    [
        ("incomplete", "Infrastructure", True),
        ("incomplete", "Job", False),
        ("incomplete", None, False),
        ("pass", None, False),
        ("fail", "Infrastructure", False),
    ],
)
def test_telemetry_uses_normalized_classification(
    callback_module, canceled_job, result, error_code, expected
):
    _, node, _, emitter = canceled_job
    node["result"] = result
    node["data"]["error_code"] = error_code
    hierarchy = {
        "child_nodes": [
            {"node": {"kind": "test", "name": "example", "result": "pass"}}
        ]
    }

    callback_module._emit_callback_telemetry(node, hierarchy)

    calls = emitter.emit.call_args_list
    assert calls[0].args == ("job_result",)
    assert calls[0].kwargs["is_infra_error"] is expected
    assert calls[1].args == ("test_result",)


def test_telemetry_failure_does_not_fail_persisted_callback(
    callback_module, canceled_job
):
    helper, node, callback, emitter = canceled_job
    emitter.emit.side_effect = RuntimeError("telemetry unavailable")

    callback_module.async_job_submit(helper, node["id"], callback)

    helper.submit_results.assert_called_once()
    callback_module.metrics.add.assert_called_once_with(
        "lava_callback_telemetry_fail_total", 1
    )
    callback_module.logger.info.assert_any_call(
        f"Completed processing callback for node {node['id']}"
    )


def test_result_submission_failure_still_counts_as_callback_failure(
    callback_module, canceled_job
):
    helper, node, callback, emitter = canceled_job
    helper.submit_results.side_effect = RuntimeError("API unavailable")

    callback_module.async_job_submit(helper, node["id"], callback)

    callback_module.metrics.add.assert_called_once_with(
        "lava_callback_late_fail_total", 1
    )
    emitter.emit.assert_not_called()
