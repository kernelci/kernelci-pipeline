#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later
"""Reconcile running pipeline nodes with their Kubernetes Jobs."""

import collections
import datetime
import json
import pathlib
import tempfile
import time

from kubernetes import client, config

API_TIMEOUT = (5, 20)
LOG_LIMIT_BYTES = 4 * 1024 * 1024


def job_outcome(job, pods):
    """Return a reconciliation error, or None while execution can continue.

    A failed attempt is not a failed Job: the controller may retry it. Likewise,
    Pending pods must not be treated as missing or failed. A successful Job
    without a pipeline result is incomplete, never an inferred kernel pass.
    """
    if job is not None and job.status is not None:
        for condition in job.status.conditions or []:
            if condition.status != "True":
                continue
            if condition.type == "Failed":
                return "k8s_job_failed", condition.reason or "Job failed"
            if condition.type == "Complete":
                return "k8s_result_missing", "Job completed without a result"
    if any(
        pod.status.phase in ("Pending", "Running", "Unknown") for pod in pods
    ):
        return None
    if job is None:
        return "k8s_job_missing", "Job disappeared from the cluster"
    if not pods:
        return "k8s_pod_missing", "Job has no pods"
    # Let the Job controller settle/retry rather than infer failure from an
    # individual pod, even if every currently visible attempt has terminated.
    return None


def pod_details(pod):
    """Diagnostic status only; never upload pod specs containing credentials."""
    statuses = (pod.status.init_container_statuses or []) + (
        pod.status.container_statuses or []
    )
    return {
        "name": pod.metadata.name,
        "uid": pod.metadata.uid,
        "node": pod.spec.node_name,
        "phase": pod.status.phase,
        "reason": pod.status.reason,
        "message": pod.status.message,
        "conditions": [c.to_dict() for c in pod.status.conditions or []],
        "containers": [
            {
                "name": status.name,
                "restart_count": status.restart_count,
                "state": status.state.to_dict() if status.state else None,
                "last_state": status.last_state.to_dict()
                if status.last_state
                else None,
            }
            for status in statuses
        ],
    }


class K8sMonitor:
    """Polling worker owned by the Kubernetes scheduler, not a service."""

    def __init__(self, api, storage, log, runtime_names, heartbeat):
        self._api = api
        self._storage = storage
        self.log = log
        self._owner = self._api.user.whoami()["username"]
        self._runtime_names = set(runtime_names)
        self._heartbeat = heartbeat
        self._clients = {}
        self._observations = {}
        self._namespace = "default"
        self._grace_seconds = 120

    def _cluster(self, context):
        if context not in self._clients:
            # Isolated ApiClients avoid changing the scheduler's/global context.
            self._clients[context] = config.new_client_from_config(
                context=context
            )
        api_client = self._clients[context]
        return client.BatchV1Api(api_client), client.CoreV1Api(api_client)

    def close(self):
        for api_client in self._clients.values():
            api_client.close()

    def _eligible(self, node):
        data = node.get("data", {})
        return (
            node.get("owner") == self._owner
            and node.get("state") == "running"
            and node.get("result") is None
            and data.get("platform") == "kubernetes"
            and data.get("runtime") in self._runtime_names
            and bool(data.get("job_id"))
            and bool(data.get("job_context"))
        )

    def poll(self):
        self._heartbeat("finding running Kubernetes nodes")
        groups = collections.defaultdict(list)
        live_ids = set()
        for node in self._api.node.find(
            {
                "owner": self._owner,
                "state": "running",
                "data.platform": "kubernetes",
            }
        ):
            if self._eligible(node):
                groups[node["data"]["job_context"]].append(node)
                live_ids.add(node["id"])
        for node_id in set(self._observations) - live_ids:
            self._observations.pop(node_id)
        for context, nodes in groups.items():
            self._poll_cluster(context, nodes)

    def _poll_cluster(self, context, nodes):
        self._heartbeat(f"inspecting Kubernetes cluster {context}")
        try:
            batch, core = self._cluster(context)
            jobs = self._list_items(batch.list_namespaced_job)
            pods = self._list_items(core.list_namespaced_pod)
        except Exception as error:
            # A failed/unauthorized/incomplete query is not evidence of absence.
            self.log.warning(
                f"Cannot inspect Kubernetes cluster {context}: {error}"
            )
            for node in nodes:
                self._observations.pop(node["id"], None)
            return
        by_job = {job.metadata.name: job for job in jobs}
        by_pod = collections.defaultdict(list)
        for pod in pods:
            name = (pod.metadata.labels or {}).get("job-name")
            if name:
                by_pod[name].append(pod)
        for node in nodes:
            self._heartbeat(f"checking Kubernetes node {node['id']}")
            try:
                self._check(node, by_job, by_pod, core)
            except Exception:
                # One upload or node update must not prevent other reconciliation.
                self.log.traceback()

    def _list_items(self, method):
        items = []
        continuation = None
        while True:
            self._heartbeat("listing Kubernetes namespace resources")
            page = method(
                self._namespace,
                limit=500,
                _continue=continuation,
                _request_timeout=API_TIMEOUT,
            )
            items.extend(page.items)
            token = page.metadata._continue if page.metadata else None
            if not token:
                return items
            if token == continuation:
                raise RuntimeError("Kubernetes pagination did not advance")
            continuation = token

    def _check(self, node, jobs, pods, core):
        job_name = node["data"]["job_id"]
        job = jobs.get(job_name)
        attempts = pods.get(job_name, [])
        if job is not None:
            attempts = [
                pod
                for pod in attempts
                if any(
                    ref.uid == job.metadata.uid
                    for ref in pod.metadata.owner_references or []
                )
            ]
        outcome = job_outcome(job, attempts)
        if outcome is None:
            self._observations.pop(node["id"], None)
            return
        # Require at least two successful observations separated by a grace
        # period, also after restart. This covers submission/controller delay
        # and gives in-flight result publication time to complete.
        signature = (node["data"]["job_context"], job_name, outcome[0])
        previous = self._observations.get(node["id"])
        now = time.monotonic()
        if previous is None or previous[0] != signature:
            self._observations[node["id"]] = (signature, now)
            return
        if now - previous[1] < self._grace_seconds:
            return
        current = self._current(node)
        if current is None:
            self._observations.pop(node["id"], None)
            return
        artifacts = self._diagnostics(current, job, attempts, core, outcome)
        # Uploads may take time. Preserve a build result that arrived meanwhile.
        current = self._current(node)
        if current is None:
            self._observations.pop(node["id"], None)
            return
        current.setdefault("artifacts", {}).update(artifacts)
        current["data"].update(error_code=outcome[0], error_msg=outcome[1])
        current.update(state="done", result="incomplete")
        self._api.node.update(current)
        self._observations.pop(node["id"], None)
        self.log.warning(
            f"{node['id']} {job_name}: {outcome[0]} ({outcome[1]})"
        )

    def _current(self, original):
        node = self._api.node.get(original["id"])
        if not node or not self._eligible(node):
            return None
        for field in ("job_id", "job_context"):
            if node["data"][field] != original["data"][field]:
                return None
        return node

    def _diagnostics(self, node, job, pods, core, outcome):
        status = {
            "observed": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "node_id": node["id"],
            "context": node["data"]["job_context"],
            "namespace": self._namespace,
            "job_name": node["data"]["job_id"],
            "error_code": outcome[0],
            "error_msg": outcome[1],
            "job_status": job.status.to_dict() if job is not None else None,
            "pods": [pod_details(pod) for pod in pods],
        }
        logs = [f"{outcome[0]}: {outcome[1]}\n"]
        for pod in pods:
            statuses = (pod.status.init_container_statuses or []) + (
                pod.status.container_statuses or []
            )
            for container in statuses:
                self._pod_logs(core, pod, container, logs)
        if not pods:
            logs.append("No pods remain; their logs cannot be recovered.\n")
        # Upload diagnostics before finishing the node. On storage failure the
        # next poll retries while Kubernetes still retains its Job and pods.
        with tempfile.TemporaryDirectory() as directory:
            paths = {
                "kubernetes_log": ("kubernetes.log", "\n".join(logs)),
                "kubernetes_status": (
                    "kubernetes-status.json",
                    json.dumps(status, indent=2, default=str),
                ),
            }
            artifacts = {}
            destination = f"{node['name']}-{node['id']}/kubernetes"
            for key, (filename, content) in paths.items():
                self._heartbeat(f"uploading {filename} for {node['id']}")
                path = pathlib.Path(directory, filename)
                path.write_text(content, encoding="utf-8")
                url = self._storage.upload_single(
                    (str(path), filename), destination
                )
                if not url:
                    raise RuntimeError(
                        f"No artifact URL returned for {filename}"
                    )
                artifacts[key] = url
            return artifacts

    def _pod_logs(self, core, pod, container, logs):
        for previous in [False, True] if container.restart_count else [False]:
            self._heartbeat(f"fetching logs for pod {pod.metadata.name}")
            logs.append(
                f"--- pod={pod.metadata.name} container={container.name} "
                f"previous={previous} (tail, up to {LOG_LIMIT_BYTES} bytes) ---"
            )
            try:
                logs.append(
                    core.read_namespaced_pod_log(
                        pod.metadata.name,
                        self._namespace,
                        container=container.name,
                        previous=previous,
                        timestamps=True,
                        tail_lines=10000,
                        limit_bytes=LOG_LIMIT_BYTES,
                        _request_timeout=API_TIMEOUT,
                    )
                )
            except Exception as error:
                # Evicted/deleted pods and containers that never started may
                # have no logs. Keep the termination status and explain the gap.
                logs.append(f"Logs unavailable: {error}")
