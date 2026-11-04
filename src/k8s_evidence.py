# SPDX-License-Identifier: LGPL-2.1-or-later
"""Bounded, restart-safe diagnostic history for Kubernetes reconciliation."""

import collections
import datetime
import hashlib
import json
import pathlib
import shutil
import tempfile
import time

MAX_PODS = 100
MAX_EVENTS = 200
MAX_SNAPSHOTS = 100
MAX_CHECKPOINT_BYTES = 1024 * 1024
MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
MAX_CHECKPOINT_FILES = 1024
MAX_CACHED_BUILDS = 128
MIN_FREE_BYTES = 16 * 1024 * 1024
TEMP_MAX_AGE = 3600


class EventIndex:
    """Index a cluster's event batch once instead of scanning it per build."""

    def __init__(self, events=()):
        self.by_uid = collections.defaultdict(list)
        self.by_name = collections.defaultdict(list)
        for event in events:
            ref = event["object"]
            self.by_uid[(ref["kind"], ref["namespace"], ref["uid"])].append(
                event
            )
            self.by_name[(ref["kind"], ref["namespace"], ref["name"])].append(
                event
            )

    def uid(self, kind, namespace, uid):
        return self.by_uid.get((kind, namespace, uid), ())

    def name(self, kind, namespace, name):
        return self.by_name.get((kind, namespace, name), ())


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def timestamp(value):
    """Normalize API timestamps, including naive pipeline creation times."""
    if not value:
        return None
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc).isoformat()


def event_details(event):
    """Keep diagnostic fields only, not arbitrary event metadata."""
    ref = event.involved_object
    series = event.series
    return {
        "uid": event.metadata.uid,
        "object": {
            "kind": ref.kind,
            "name": ref.name,
            "namespace": ref.namespace,
            "uid": ref.uid,
        },
        "type": event.type,
        "reason": event.reason,
        "message": (event.message or "")[:4096],
        "first_seen": timestamp(event.first_timestamp or event.event_time),
        "last_seen": timestamp(
            (series.last_observed_time if series else None)
            or event.last_timestamp
            or event.event_time
            or event.metadata.creation_timestamp
        ),
        "count": series.count if series else event.count,
        "reporter": event.reporting_component
        or (event.source.component if event.source else None),
    }


class Evidence:
    """Remember identities and observations even after the pods disappear."""

    def __init__(self, node, namespace, job_uid=None, saved=None):
        self.identity = [
            node["id"],
            node["data"]["job_context"],
            namespace,
            node["data"]["job_id"],
        ]
        self.data = {
            "identity": self.identity,
            "job_uid": job_uid,
            "since": timestamp(node.get("created")) or utc_now(),
            "pods": {},
            "snapshots": [],
            "events": {},
            "event_query_error": None,
            "truncated": False,
        }
        if isinstance(saved, dict) and saved.get("identity") == self.identity:
            for key in self.data:
                if key not in saved:
                    raise ValueError(f"Incomplete evidence checkpoint: {key}")
            self.data = saved
        self.set_job(job_uid)

    def set_job(self, job_uid):
        if not job_uid:
            return
        if self.data["job_uid"] not in (None, job_uid):
            # Reused names must not inherit another Kubernetes Job's evidence.
            self.data.update(pods={}, snapshots=[], events={}, truncated=False)
        self.data["job_uid"] = job_uid

    def observe_pod(self, details):
        details = json.loads(json.dumps(details, default=str))
        uid = details["uid"]
        pods = self.data["pods"]
        if pods.get(uid) == details:
            return
        pods[uid] = details
        self.data["snapshots"].append({"observed": utc_now(), **details})
        self._trim(pods, MAX_PODS)
        if len(self.data["snapshots"]) > MAX_SNAPSHOTS:
            self.data["snapshots"] = self.data["snapshots"][-MAX_SNAPSHOTS:]
            self.data["truncated"] = True

    def _trim(self, entries, limit):
        while len(entries) > limit:
            del entries[next(iter(entries))]
            self.data["truncated"] = True

    def observe_events(self, events, error=None):
        self.data["event_query_error"] = error
        index = events if isinstance(events, EventIndex) else EventIndex(events)
        namespace = self.identity[2]
        job_events = index.uid("Job", namespace, self.data["job_uid"])
        # A retained SuccessfulCreate event can identify a pod which vanished
        # between polls. Establish this link from the exact Job UID first.
        self._retain_events(job_events, set(), set(), set())
        known_names = {pod["name"] for pod in self.data["pods"].values()}
        workers = {pod["node"] for pod in self.data["pods"].values()} - {None}
        created_names = {
            event["message"][len("Created pod: ") :]
            for event in self.data["events"].values()
            if event["scope"] == "job"
            and event["reason"] == "SuccessfulCreate"
            and event["message"].startswith("Created pod: ")
        } - known_names
        candidates = []
        for uid in self.data["pods"]:
            candidates.extend(index.uid("Pod", namespace, uid))
        for name in created_names:
            candidates.extend(index.name("Pod", namespace, name))
        for name in workers:
            # Node event object references are cluster-scoped.
            candidates.extend(index.name("Node", None, name))
            candidates.extend(index.name("Node", "", name))
            candidates.extend(index.name("Node", namespace, name))
        self._retain_events(candidates, known_names, workers, created_names)

    def _retain_events(self, events, known_names, workers, created_names):
        for event in events:
            scope = self._event_scope(
                event, known_names, workers, created_names
            )
            if not scope:
                continue
            # Replace aggregated events by UID, preserving their updated count.
            self.data["events"][event["uid"]] = {**event, "scope": scope}
            self._trim(self.data["events"], MAX_EVENTS)

    def _event_scope(self, event, known_names, workers, created_names):
        ref = event["object"]
        if not event["uid"] or not ref["uid"]:
            return None
        if (
            ref["kind"] in ("Pod", "Job")
            and ref["namespace"] != self.identity[2]
        ):
            return None
        if event["last_seen"] and event["last_seen"] < self.data["since"]:
            return None
        if ref["kind"] == "Job" and ref["uid"] == self.data["job_uid"]:
            return "job"
        if ref["kind"] == "Pod" and ref["uid"] in self.data["pods"]:
            return "pod"
        if (
            ref["kind"] == "Pod"
            and ref["name"] not in known_names
            and ref["name"] in created_names
        ):
            return "pod"
        # Node events are context, not proof of the cause of a pod failure.
        # Match only nodes observed hosting this build and events in its window.
        if (
            ref["kind"] == "Node"
            and ref["name"] in workers
            and event["last_seen"]
        ):
            return "node_context"
        return None

    def encode(self):
        """Enforce a byte limit as well as entry counts, including in memory."""
        while True:
            payload = json.dumps(
                self.data, ensure_ascii=False, default=str
            ).encode("utf-8")
            if len(payload) <= MAX_CHECKPOINT_BYTES:
                return payload
            self.data["truncated"] = True
            # Drop older observations in batches; never loop over serialized
            # bytes one at a time when a pod contains an unusually large status.
            for key in ("snapshots", "events", "pods"):
                entries = self.data[key]
                if not entries:
                    continue
                count = max(1, len(entries) // 2)
                if isinstance(entries, list):
                    del entries[:count]
                else:
                    for uid in list(entries)[:count]:
                        del entries[uid]
                break
            else:
                raise ValueError(
                    "Evidence identity exceeds checkpoint byte limit"
                )

    def events(self):
        return sorted(
            self.data["events"].values(),
            key=lambda event: (event["last_seen"] or "", event["uid"]),
        )

    def summary(self):
        lines = []
        for pod in self.data["snapshots"]:
            lines.append(
                f"Observed pod {pod['name']} at {pod['observed']}: "
                f"phase={pod['phase']} reason={pod['reason']} "
                f"message={pod['message']}"
            )
            for condition in pod["conditions"]:
                if condition.get("reason") or condition.get("message"):
                    lines.append(f"  condition={json.dumps(condition)}")
            for container in pod["containers"]:
                lines.append(
                    f"  container={container['name']} "
                    f"state={json.dumps(container['state'])} "
                    f"last_state={json.dumps(container['last_state'])}"
                )
        for event in self.events():
            lines.append(
                f"Event {event['last_seen']} [{event['scope']}] "
                f"{event['object']['kind']}/{event['object']['name']} "
                f"{event['reason']} (count={event['count']}): {event['message']}"
            )
        if self.data["event_query_error"]:
            lines.append(
                f"Events unavailable: {self.data['event_query_error']}"
            )
        if not self.data["snapshots"] and not self.data["events"]:
            lines.append(
                "Underlying cause unknown; no retained pod status or related "
                "events found."
            )
        if self.data["truncated"]:
            lines.append(
                "Diagnostic history truncated to its retention limits."
            )
        return lines


class EvidenceStore:
    """Checkpoint status before the confirmation delay; never store pod specs."""

    def __init__(self, directory):
        self.directory = pathlib.Path(directory)
        self.cache = collections.OrderedDict()
        self._digests = {}

    @staticmethod
    def filename(node_id):
        return hashlib.sha256(node_id.encode()).hexdigest() + ".json"

    def get(self, node, namespace, job_uid):
        identity = (
            node["id"],
            node["data"]["job_context"],
            namespace,
            node["data"]["job_id"],
        )
        evidence = self.cache.get(node["id"])
        if evidence is None or tuple(evidence.identity) != identity:
            path = self.directory / self.filename(node["id"])
            saved = None
            if path.exists():
                with path.open("rb") as stream:
                    payload = stream.read(MAX_CHECKPOINT_BYTES + 1)
                if len(payload) > MAX_CHECKPOINT_BYTES:
                    raise ValueError("Evidence checkpoint exceeds byte limit")
                saved = json.loads(payload)
                self._digests[path.name] = hashlib.sha256(payload).digest()
            evidence = Evidence(node, namespace, job_uid, saved)
            self.remember(node["id"], evidence)
        evidence.set_job(job_uid)
        self.cache.move_to_end(node["id"])
        return evidence

    def remember(self, node_id, evidence):
        self.cache[node_id] = evidence
        self.cache.move_to_end(node_id)
        while len(self.cache) > MAX_CACHED_BUILDS:
            self.cache.popitem(last=False)

    def save(self, evidence):
        payload = evidence.encode()
        self.directory.mkdir(parents=True, exist_ok=True)
        destination = self.directory / self.filename(evidence.identity[0])
        digest = hashlib.sha256(payload).digest()
        if (
            self._digests.get(destination.name) == digest
            and destination.exists()
        ):
            return
        files = list(self.directory.iterdir())
        used = sum(path.stat().st_size for path in files if path.is_file())
        # Include the existing destination and temporary replacement in the
        # budget. This directory has one writer: the scheduler's monitor.
        if used + len(payload) > MAX_DIRECTORY_BYTES:
            raise OSError("Kubernetes evidence directory byte budget exhausted")
        if len(files) >= MAX_CHECKPOINT_FILES:
            raise OSError(
                "Kubernetes evidence checkpoint count budget exhausted"
            )
        if shutil.disk_usage(self.directory).free < MIN_FREE_BYTES + len(
            payload
        ):
            raise OSError("Insufficient free disk for Kubernetes evidence")
        # Atomic replacement prevents a restart reading a partial checkpoint.
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=self.directory,
            prefix="evidence-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            path = pathlib.Path(stream.name)
            try:
                stream.write(payload)
                stream.flush()
                path.replace(destination)
                self._digests[destination.name] = digest
            finally:
                path.unlink(missing_ok=True)

    def prune(self, live_ids):
        for node_id in set(self.cache) - live_ids:
            del self.cache[node_id]
        live_files = {self.filename(node_id) for node_id in live_ids}
        for path in self.directory.iterdir() if self.directory.exists() else ():
            stale_temp = (
                (path.name.startswith("evidence-") and path.suffix == ".tmp")
                or path.name.startswith(
                    "tmp"
                )  # Checkpoints from the first version.
            ) and time.time() - path.stat().st_mtime > TEMP_MAX_AGE
            if stale_temp or (
                path.suffix == ".json" and path.name not in live_files
            ):
                path.unlink()
                self._digests.pop(path.name, None)
        self._digests = {
            name: digest
            for name, digest in self._digests.items()
            if name in live_files
        }
