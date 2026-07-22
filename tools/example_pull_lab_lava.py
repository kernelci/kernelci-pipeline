#!/usr/bin/env python3
"""
Poll KernelCI API for LAVA pull-lab jobs and run them on a local LAVA
instance.

This is a companion to example_pull_lab.py for labs that run LAVA behind
a firewall.  Such labs cannot use the regular push-style LAVA integration
because the KernelCI pipeline has no URL to reach them.  Instead, a LAVA
runtime is configured in the pipeline *without* a `url` (see the
`lava-testpull` entry in config/pipeline.yaml): the pipeline then renders
the complete LAVA job definition, stores it in external storage and
publishes its URL as the `job_definition` artifact of the job node.

This script pulls those jobs:

1. Poll the KernelCI events API for job nodes with a `job_definition`
   artifact pointing at a LAVA YAML definition.
2. Download the definition and submit it verbatim to the local LAVA
   instance via its REST API.

No result handling is needed here: the definition already contains a
`notify` callback pointing back at the KernelCI pipeline, so LAVA posts
its standard callback (outbound HTTP) when the job finishes, just like
push-style LAVA labs do.  The callback token name in the definition
(e.g. `kernelci-lab-testpull`) must exist as a "remote token" in the
LAVA profile of the submitting user, holding the secret value shared
with the KernelCI pipeline admins.

Submitted jobs and the last seen event timestamp are persisted in an
append-only JSONL journal (see --journal-file), so restarting the
script neither resubmits jobs nor replays old events.  The journal
tolerates interrupted writes and is compacted automatically.

Examples:
    export LAVA_TOKEN=<lava api token>

    # Poll for jobs and submit them to the local LAVA instance
    python tools/example_pull_lab_lava.py \\
        --lava-url https://lava.mylab.local --lab-name lava-testpull

    # Inspect a job definition without submitting it
    python tools/example_pull_lab_lava.py --job-yaml job.yaml --dry-run
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

EVENTS_PATH = "/events"
POLL_PERIOD = 30  # seconds between event polls
MAX_JOURNAL_ENTRIES = 5000  # compact the journal when it grows past this
KEEP_JOURNAL_ENTRIES = 1000  # entries kept after compaction


def pollevents(api_url, timestamp, kind):
    url = (
        api_url
        + EVENTS_PATH
        + f"?state=done&kind={kind}&limit=1000&recursive=true&from={timestamp}"
    )
    print(url)
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def retrieve_job_definition(url):
    print(f"Retrieving job definition from: {url}")
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response.text


class Journal:
    """Crash-resilient JSONL journal of submitted jobs

    Restarting the script must not resubmit jobs that already went to
    LAVA.  The journal is an append-only JSONL file with one entry per
    submitted job ({"node_id": ..., "timestamp": ...}) and one
    checkpoint of the last seen event timestamp ({"timestamp": ...})
    per poll batch.  Every append is flushed and fsynced, so a crash
    can at most lose the last partial line, which is skipped when the
    journal is loaded.  The file is compacted on startup and whenever
    it grows past MAX_JOURNAL_ENTRIES: only the newest
    KEEP_JOURNAL_ENTRIES are kept, which is safe because polling
    resumes from the last checkpoint and older events are never seen
    again.
    """

    def __init__(self, path):
        self.path = path
        self.timestamp = None
        self.processed = set()
        self._entries = []
        self._load()
        if len(self._entries) > KEEP_JOURNAL_ENTRIES:
            self._compact()
        elif self._needs_newline():
            # Terminate a line torn by a crash so the next append does
            # not get corrupted by it
            with open(self.path, "a", encoding="utf-8") as journal_file:
                journal_file.write("\n")
        self._file = open(self.path, "a", encoding="utf-8")

    def _needs_newline(self):
        try:
            with open(self.path, "rb") as journal_file:
                journal_file.seek(-1, os.SEEK_END)
                return journal_file.read(1) != b"\n"
        except (FileNotFoundError, OSError):
            return False

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as journal_file:
                for line in journal_file:
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        # Partial line from an interrupted write
                        continue
                    self._entries.append(entry)
        except FileNotFoundError:
            return
        for entry in self._entries:
            if entry.get("timestamp"):
                self.timestamp = entry["timestamp"]
            if entry.get("node_id"):
                self.processed.add(entry["node_id"])

    def _compact(self):
        self._entries = self._entries[-KEEP_JOURNAL_ENTRIES:]
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as tmp_file:
            for entry in self._entries:
                tmp_file.write(json.dumps(entry) + "\n")
        os.replace(tmp_path, self.path)

    def _append(self, entry):
        self._entries.append(entry)
        self._file.write(json.dumps(entry) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        if len(self._entries) > MAX_JOURNAL_ENTRIES:
            self._file.close()
            self._compact()
            self._file = open(self.path, "a", encoding="utf-8")

    def mark(self, node_id):
        """Remember a node in memory only (skipped or dry-run jobs)"""
        self.processed.add(node_id)

    def record(self, node_id, timestamp):
        """Persist a submitted job so restarts do not resubmit it"""
        self.processed.add(node_id)
        if timestamp:
            self.timestamp = timestamp
        self._append({"node_id": node_id, "timestamp": timestamp})

    def checkpoint(self, timestamp):
        """Persist the last seen event timestamp"""
        self.timestamp = timestamp
        self._append({"timestamp": timestamp})


def parse_definition(definition):
    """Parse the LAVA job definition, return it as a dictionary

    Returns None if the definition is not valid YAML or does not look
    like a LAVA job.
    """
    try:
        job = yaml.safe_load(definition)
    except yaml.YAMLError as exc:
        print(f"Invalid YAML in job definition: {exc}")
        return None
    if not isinstance(job, dict) or "device_type" not in job:
        print("Job definition does not look like a LAVA job")
        return None
    return job


def submit_to_lava(lava_url, lava_token, definition):
    """Submit a LAVA job definition, return the LAVA job ID"""
    url = lava_url.rstrip("/") + "/api/v0.2/jobs/"
    response = requests.post(
        url,
        headers={"Authorization": f"Token {lava_token}"},
        json={"definition": definition},
        timeout=60,
    )
    if response.status_code >= 400:
        print(f"Error submitting job: {response.status_code} {response.text}")
    response.raise_for_status()
    data = response.json()
    job_ids = data.get("job_ids") or [data.get("job_id")]
    return job_ids[0]


def wait_for_lava_job(lava_url, lava_token, job_id, timeout_s):
    """Poll the LAVA job until it finishes, return its health"""
    url = lava_url.rstrip("/") + f"/api/v0.2/jobs/{job_id}/"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        response = requests.get(
            url, headers={"Authorization": f"Token {lava_token}"}, timeout=60
        )
        response.raise_for_status()
        data = response.json()
        state = data.get("state")
        health = data.get("health")
        print(f"LAVA job {job_id}: state={state} health={health}")
        if state == "Finished":
            return health
        time.sleep(POLL_PERIOD)
    print(f"Timed out waiting for LAVA job {job_id}")
    return None


def process_definition(definition, args):
    """Validate one LAVA job definition and submit it to LAVA

    Returns True if the job was actually submitted.
    """
    job = parse_definition(definition)
    if not job:
        return False

    device_type = job.get("device_type")
    print(f"Job name: {job.get('job_name')}")
    print(f"Device type: {device_type}")
    callback = job.get("notify", {}).get("callback", {})
    if callback:
        print(
            f"Callback: {callback.get('url')} "
            f"(token name: {callback.get('token')})"
        )
    else:
        print(
            "Warning: no notify callback in job definition, "
            "results will not be reported to KernelCI"
        )

    if args.device_types and device_type not in args.device_types:
        print(
            f"Skipping job - device type '{device_type}' not in "
            f"{args.device_types}"
        )
        return False

    if args.dry_run:
        print("Dry run, not submitting:")
        print(definition)
        return False

    job_id = submit_to_lava(args.lava_url, args.lava_token, definition)
    job_url = args.lava_url.rstrip("/") + f"/scheduler/job/{job_id}"
    print(f"✓ Submitted LAVA job {job_id}: {job_url}")

    if args.wait:
        health = wait_for_lava_job(
            args.lava_url, args.lava_token, job_id, args.wait_timeout * 60
        )
        print(f"LAVA job {job_id} finished with health: {health}")

    return True


def process_event(event, args, journal):
    node = event.get("node", {})
    node_id = node.get("id")
    job_definition_url = node.get("artifacts", {}).get("job_definition", "")

    if not job_definition_url.startswith("http"):
        return
    # PULL_LABS protocol jobs use .json definitions, LAVA pull jobs are
    # stored as rendered LAVA YAML files
    if not job_definition_url.endswith(".yaml"):
        return
    if node_id in journal.processed:
        return

    data = event.get("data", {}).get("data", {})
    platform = data.get("platform")
    runtime = data.get("runtime")
    if runtime != args.lab_name:
        return
    if args.platforms and platform not in args.platforms:
        return

    print(
        f"Processing job {node_id} (name: {node.get('name')}, "
        f"platform: {platform}, runtime: {runtime})"
    )
    journal.mark(node_id)

    definition = retrieve_job_definition(job_definition_url)
    if process_definition(definition, args):
        journal.record(node_id, event.get("timestamp"))


def poll_loop(args):
    journal = Journal(args.journal_file)
    timestamp = args.since or journal.timestamp
    if not timestamp:
        # First run: start from the current time, do not replay history
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    print(f"Polling for events since {timestamp}")

    retry_count = 0
    while True:
        try:
            events = pollevents(args.api_url, timestamp, "job")
            retry_count = 0

            if not events:
                print(f"No new events, sleeping for {POLL_PERIOD} seconds")
                time.sleep(POLL_PERIOD)
                continue

            print(f"Got {len(events)} events")
            for event in events:
                try:
                    process_event(event, args, journal)
                except Exception as e:
                    print(f"Error processing event: {e}")
                    import traceback

                    traceback.print_exc()

            timestamp = events[-1]["timestamp"]
            journal.checkpoint(timestamp)
        except requests.exceptions.RequestException as e:
            retry_count += 1
            print(
                f"Error fetching events "
                f"(attempt {retry_count}/{args.max_retries}): {e}"
            )
            if retry_count >= args.max_retries:
                print(f"Max retries ({args.max_retries}) reached. Exiting.")
                sys.exit(1)
            print(f"Retrying in {POLL_PERIOD} seconds...")
            time.sleep(POLL_PERIOD)


def main():
    parser = argparse.ArgumentParser(
        description="Run KernelCI LAVA pull-lab jobs on a local LAVA instance."
    )
    parser.add_argument(
        "--api-url",
        default="https://staging.kernelci.org:9000/latest",
        help="KernelCI API base URL to poll for events",
    )
    parser.add_argument(
        "--lava-url",
        help="Base URL of the local LAVA instance "
        "(e.g. https://lava.mylab.local)",
    )
    parser.add_argument(
        "--lava-token",
        default=os.environ.get("LAVA_TOKEN"),
        help="LAVA API token (default: LAVA_TOKEN environment variable)",
    )
    parser.add_argument(
        "--lab-name",
        "--runtime",
        dest="lab_name",
        help="Lab (runtime) name to pull jobs for, e.g. lava-testpull. "
        "Required in poll mode so only jobs scheduled for this lab are "
        "submitted.",
    )
    parser.add_argument(
        "--platform",
        dest="platforms",
        action="append",
        help="Only pull jobs for this platform (e.g. beaglebone-black), "
        "may be given multiple times. If not specified, accepts all "
        "platforms.",
    )
    parser.add_argument(
        "--device-type",
        dest="device_types",
        action="append",
        help="Only submit jobs for this LAVA device type, may be given "
        "multiple times. If not specified, accepts all device types.",
    )
    parser.add_argument(
        "--job-yaml",
        help="Process a single LAVA job definition from a local YAML file "
        "or URL instead of polling for events, then exit",
    )
    parser.add_argument(
        "--journal-file",
        default="example_pull_lab_lava.jsonl",
        help="JSONL journal persisting submitted jobs and the polling "
        "position, so restarts do not resubmit jobs "
        "(default: example_pull_lab_lava.jsonl)",
    )
    parser.add_argument(
        "--since",
        help="Poll events starting from this timestamp instead of "
        "resuming from the journal (e.g. 2026-07-22T00:00:00.000000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the job definition without submitting it",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for each submitted LAVA job to finish",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=120,
        help="Timeout in minutes when waiting for a LAVA job (default: 120)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum number of consecutive retries on API errors (default: 5)",
    )
    args = parser.parse_args()

    if not args.job_yaml and not args.lab_name:
        parser.error("--lab-name is required in poll mode")
    if not args.dry_run:
        if not args.lava_url:
            parser.error("--lava-url is required unless --dry-run is used")
        if not args.lava_token:
            parser.error(
                "LAVA token is required unless --dry-run is used, "
                "set LAVA_TOKEN or use --lava-token"
            )

    if args.job_yaml:
        if args.job_yaml.startswith("http"):
            definition = retrieve_job_definition(args.job_yaml)
        else:
            with open(args.job_yaml, encoding="utf-8") as job_file:
                definition = job_file.read()
        process_definition(definition, args)
        return

    poll_loop(args)


if __name__ == "__main__":
    main()
