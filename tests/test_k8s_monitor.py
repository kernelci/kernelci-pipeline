"""Kubernetes reconciliation must not turn healthy builds into failures."""

import copy
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from src.k8s_monitor import K8sMonitor, job_outcome
from src.scheduler import K8S_MONITOR_POLL_PERIOD, Scheduler


def make_job(condition=None):
    conditions = []
    if condition:
        conditions.append(
            client.V1JobCondition(
                type=condition, status="True", reason="BackoffLimitExceeded"
            )
        )
    return client.V1Job(
        metadata=client.V1ObjectMeta(name="kci-build", uid="job-uid"),
        status=client.V1JobStatus(conditions=conditions),
    )


def make_pod(phase="Running", reason=None, restart_count=0):
    terminated = None
    if phase == "Failed":
        terminated = client.V1ContainerStateTerminated(
            exit_code=137, reason=reason or "OOMKilled"
        )
    return client.V1Pod(
        metadata=client.V1ObjectMeta(
            name="kci-build-pod",
            uid="pod-uid",
            labels={"job-name": "kci-build"},
            owner_references=[
                client.V1OwnerReference(
                    api_version="batch/v1",
                    kind="Job",
                    name="kci-build",
                    uid="job-uid",
                )
            ],
        ),
        spec=client.V1PodSpec(
            node_name="worker",
            containers=[
                client.V1Container(
                    name="kernelci",
                    env=[
                        client.V1EnvVar(
                            name="SECRET", value="must-not-upload-pod-spec"
                        )
                    ],
                )
            ],
        ),
        status=client.V1PodStatus(
            phase=phase,
            reason=reason,
            container_statuses=[
                client.V1ContainerStatus(
                    name="kernelci",
                    image="image",
                    image_id="image-id",
                    ready=False,
                    restart_count=restart_count,
                    state=client.V1ContainerState(terminated=terminated),
                )
            ],
        ),
    )


def node_page(items, token=None):
    return type(
        "Page",
        (),
        {"items": items, "metadata": client.V1ListMeta(_continue=token)},
    )()


@pytest.fixture
def monitor():
    api, storage, log = MagicMock(), MagicMock(), MagicMock()
    api.user.whoami.return_value = {"username": "production"}
    worker = K8sMonitor(api, storage, log, {"k8s-all"}, MagicMock())
    node = {
        "id": "node-1",
        "name": "kbuild-test",
        "state": "running",
        "result": None,
        "owner": "production",
        "data": {
            "platform": "kubernetes",
            "runtime": "k8s-all",
            "job_id": "kci-build",
            "job_context": "cluster-a",
        },
        "artifacts": {"existing": "https://storage/existing"},
    }
    api.node.find.return_value = [node]
    api.node.get.side_effect = lambda _: copy.deepcopy(node)
    worker.node = node
    worker.clock = 0
    worker.saved = {}

    def upload(source, destination):
        path, filename = source
        worker.saved[filename] = Path(path).read_text()
        return f"https://storage/{destination}/{filename}"

    storage.upload_single.side_effect = upload
    worker.core = MagicMock()
    worker.core.read_namespaced_pod_log.return_value = "fatal worker error\n"
    return worker


def check(worker, job=None, pods=None, seconds=0):
    worker.clock += seconds
    jobs = {"kci-build": job} if job is not None else {}
    with patch("src.k8s_monitor.time.monotonic", return_value=worker.clock):
        worker._check(worker.node, jobs, {"kci-build": pods or []}, worker.core)


@pytest.mark.parametrize("phase", ["Running", "Pending", "Unknown"])
def test_nonterminal_pod_is_not_missing_even_without_job(phase):
    assert job_outcome(None, [make_pod(phase)]) is None


def test_failed_attempt_waits_for_kubernetes_retry(monitor):
    job, pods = make_job(), [make_pod("Failed")]
    check(monitor, job, pods)
    check(monitor, job, pods, 600)
    monitor._api.node.update.assert_not_called()
    monitor._storage.upload_single.assert_not_called()


@pytest.mark.parametrize(
    "condition,code",
    [
        ("Failed", "k8s_job_failed"),
        ("Complete", "k8s_result_missing"),
    ],
)
def test_terminal_job_uploads_logs_and_status_before_completing(
    monitor, condition, code
):
    job, pods = make_job(condition), [make_pod("Failed", "Evicted")]
    check(monitor, job, pods)
    monitor._api.node.update.assert_not_called()
    check(monitor, job, pods, 600)
    result = monitor._api.node.update.call_args.args[0]
    assert result["state"] == "done"
    assert result["result"] == "incomplete"
    assert result["data"]["error_code"] == code
    assert result["artifacts"]["existing"] == "https://storage/existing"
    assert result["artifacts"]["kubernetes_log"].endswith("kubernetes.log")
    assert "fatal worker error" in monitor.saved["kubernetes.log"]
    status = json.loads(monitor.saved["kubernetes-status.json"])
    assert status["pods"][0]["reason"] == "Evicted"
    assert (
        status["pods"][0]["containers"][0]["state"]["terminated"]["exit_code"]
        == 137
    )
    assert (
        "must-not-upload-pod-spec"
        not in monitor.saved["kubernetes-status.json"]
    )


@pytest.mark.parametrize(
    "job,code",
    [
        (None, "k8s_job_missing"),
        (make_job(), "k8s_pod_missing"),
    ],
)
def test_missing_resources_require_repeated_confirmation(monitor, job, code):
    check(monitor, job)
    check(monitor, job, seconds=119)
    monitor._api.node.update.assert_not_called()
    check(monitor, job, seconds=481)
    result = monitor._api.node.update.call_args.args[0]
    assert result["data"]["error_code"] == code
    assert "cannot be recovered" in monitor.saved["kubernetes.log"]


def test_pod_reappearing_resets_missing_grace(monitor):
    job = make_job()
    check(monitor, job)
    check(monitor, job, [make_pod()], seconds=600)
    check(monitor, job, seconds=600)
    monitor._api.node.update.assert_not_called()


def test_pod_for_different_job_uid_is_not_accepted(monitor):
    pod = make_pod()
    pod.metadata.owner_references[0].uid = "other-job"
    check(monitor, make_job(), [pod])
    check(monitor, make_job(), [pod], seconds=600)
    assert (
        monitor._api.node.update.call_args.args[0]["data"]["error_code"]
        == "k8s_pod_missing"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "done"),
        ("state", "available"),
        ("result", "pass"),
        ("owner", "another-owner"),
    ],
)
def test_normal_completion_or_other_owner_is_not_overwritten(
    monitor, field, value
):
    check(monitor)
    current = copy.deepcopy(monitor.node)
    current[field] = value
    monitor._api.node.get.side_effect = lambda _: current
    check(monitor, seconds=600)
    monitor._api.node.update.assert_not_called()
    monitor._storage.upload_single.assert_not_called()


def test_completion_during_upload_is_not_overwritten(monitor):
    check(monitor)
    complete = copy.deepcopy(monitor.node)
    complete.update(state="done", result="pass")
    monitor._api.node.get.side_effect = [copy.deepcopy(monitor.node), complete]
    check(monitor, seconds=600)
    assert monitor.saved
    monitor._api.node.update.assert_not_called()


def test_reassigned_job_is_not_finished(monitor):
    check(monitor)
    current = copy.deepcopy(monitor.node)
    current["data"]["job_id"] = "replacement-job"
    monitor._api.node.get.side_effect = lambda _: current
    check(monitor, seconds=600)
    monitor._api.node.update.assert_not_called()


def test_log_unavailable_keeps_status_and_completes(monitor):
    monitor.core.read_namespaced_pod_log.side_effect = ApiException(status=404)
    job, pods = make_job("Failed"), [make_pod("Failed")]
    check(monitor, job, pods)
    check(monitor, job, pods, 600)
    assert "Logs unavailable" in monitor.saved["kubernetes.log"]
    monitor._api.node.update.assert_called_once()


def test_fetches_previous_container_logs_after_restart(monitor):
    job, pods = make_job("Failed"), [make_pod("Failed", restart_count=1)]
    check(monitor, job, pods)
    check(monitor, job, pods, 600)
    calls = monitor.core.read_namespaced_pod_log.call_args_list
    assert [call.kwargs["previous"] for call in calls] == [False, True]


def test_storage_failure_leaves_node_running_for_retry(monitor):
    monitor._storage.upload_single.side_effect = RuntimeError("storage down")
    check(monitor)
    with pytest.raises(RuntimeError, match="storage down"):
        check(monitor, seconds=600)
    monitor._api.node.update.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [ApiException(status=403), ApiException(status=503), TimeoutError()],
)
def test_cluster_error_does_not_mean_disappeared(monitor, error):
    check(monitor)
    batch, core = MagicMock(), MagicMock()
    batch.list_namespaced_job.side_effect = error
    with patch.object(monitor, "_cluster", return_value=(batch, core)):
        monitor._poll_cluster("cluster-a", [monitor.node])
    assert monitor._observations == {}
    check(monitor, seconds=600)
    monitor._api.node.update.assert_not_called()


def test_poll_fetches_each_cluster_once_and_ignores_unsubmitted_nodes(monitor):
    second = copy.deepcopy(monitor.node)
    second["id"] = "node-2"
    unsubmitted = copy.deepcopy(monitor.node)
    del unsubmitted["data"]["job_id"]
    other_runtime = copy.deepcopy(monitor.node)
    other_runtime["data"]["runtime"] = "other-scheduler"
    monitor._api.node.find.return_value = [
        monitor.node,
        second,
        unsubmitted,
        other_runtime,
    ]
    with patch.object(monitor, "_poll_cluster") as poll:
        monitor.poll()
    poll.assert_called_once_with("cluster-a", [monitor.node, second])


def test_namespace_listing_follows_pagination(monitor):
    method = MagicMock(side_effect=[node_page([1], "next"), node_page([2])])
    assert monitor._list_items(method) == [1, 2]
    assert method.call_args.kwargs["_continue"] == "next"


def test_failure_on_later_page_does_not_create_missing_observation(monitor):
    batch, core = MagicMock(), MagicMock()
    batch.list_namespaced_job.side_effect = [
        node_page([], "next"),
        TimeoutError(),
    ]
    with patch.object(monitor, "_cluster", return_value=(batch, core)):
        monitor._poll_cluster("cluster-a", [monitor.node])
    assert monitor._observations == {}


def test_restart_requires_fresh_confirmation(monitor):
    check(monitor)
    monitor._observations.clear()
    check(monitor, seconds=600)
    monitor._api.node.update.assert_not_called()


def test_scheduler_starts_worker_only_for_kubernetes():
    scheduler = MagicMock(spec=Scheduler)
    scheduler._runtimes = {"lava": MagicMock()}
    scheduler._runtimes["lava"].config.lab_type = "lava"
    scheduler._threads = []
    with patch("src.scheduler.threading.Thread") as thread:
        Scheduler._start_k8s_monitor(scheduler)
        thread.assert_not_called()
        scheduler._runtimes["k8s-all"] = MagicMock()
        scheduler._runtimes["k8s-all"].config.lab_type = "kubernetes"
        scheduler._storage = MagicMock()
        Scheduler._start_k8s_monitor(scheduler)
        thread.assert_called_once()
        assert thread.call_args.kwargs["args"] == ({"k8s-all"},)
        thread.return_value.start.assert_called_once()
    assert K8S_MONITOR_POLL_PERIOD == 600


def test_scheduler_worker_retries_initialization_and_stops_promptly():
    scheduler = MagicMock(spec=Scheduler)
    scheduler._k8s_monitor_stop = threading.Event()
    scheduler._api_config = MagicMock()
    scheduler._storage = MagicMock()
    monitor = MagicMock()
    monitor.poll.side_effect = scheduler._k8s_monitor_stop.set
    clock = [0]

    def advance(seconds):
        assert seconds <= 30  # Idle heartbeats must stay within the watchdog.
        clock[0] += 600

    with (
        patch("src.scheduler.kernelci.api.get_api"),
        patch("src.scheduler.kernelci.storage.get_storage"),
        patch(
            "src.scheduler.K8sMonitor", side_effect=[TimeoutError(), monitor]
        ) as constructor,
        patch("src.scheduler.time.monotonic", side_effect=lambda: clock[0]),
        patch.object(scheduler._k8s_monitor_stop, "wait", side_effect=advance),
    ):
        Scheduler._monitor_k8s(scheduler, {"k8s-all"})
    assert constructor.call_count == 2
    monitor.poll.assert_called_once()
    monitor.close.assert_called_once()
    scheduler.log.traceback.assert_called_once()
