#!/usr/bin/env python3
"""
Poll KernelCI API for pull-lab jobs and execute them with pytest + QEMU.

Two modes:
  poll (default) — polls KernelCI events API for new jobs
  direct         — runs pytest against a local job JSON file
"""

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
import traceback

import requests


BASE_URI = "https://staging.kernelci.org:9000/latest"
EVENTS_PATH = "/events"
REQUEST_TIMEOUT = 30

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TESTS_DIR = os.path.join(SCRIPT_DIR, "example_pytest_tests")

log = logging.getLogger(__name__)


def pollevents(timestamp, kind):
    url = (
        BASE_URI
        + EVENTS_PATH
        + f"?state=done&kind={kind}&limit=1000&recursive=true&from={timestamp}"
    )
    log.debug("Polling %s", url)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def retrieve_job_definition(url):
    log.debug("Retrieving job definition from %s", url)
    response = requests.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def run_pytest(job, tests_dir, output_dir=None):
    tmp_file = None
    try:
        if isinstance(job, dict):
            tmp_file = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", prefix="kci-job-", delete=False
            )
            json.dump(job, tmp_file)
            tmp_file.close()
            job_json_path = tmp_file.name
        else:
            job_json_path = str(job)

        cmd = [
            sys.executable,
            "-m",
            "pytest",
            f"--rootdir={tests_dir}",
            f"--kci-job-json={job_json_path}",
            "-v",
            "-s",
        ]

        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            cmd.append(f"--junitxml={os.path.join(output_dir, 'results.xml')}")

        cmd.append(tests_dir)

        log.debug("Executing: %s", " ".join(shlex.quote(arg) for arg in cmd))

        if output_dir:
            log_path = os.path.join(output_dir, "log.txt")
            with open(log_path, "w") as logfile:
                result = subprocess.run(cmd, stdout=logfile, stderr=subprocess.STDOUT)
            log.info("Log saved to %s", log_path)
        else:
            result = subprocess.run(cmd)

        log.info("pytest exited with code %d", result.returncode)
        return result.returncode

    finally:
        if tmp_file is not None:
            try:
                os.unlink(tmp_file.name)
            except OSError:
                pass


def poll_loop(args):
    timestamp = "1970-01-01T00:00:00.000000"
    retry_count = 0
    while True:
        try:
            events = pollevents(timestamp, "job")
            retry_count = 0

            if not events:
                log.debug("No new events, sleeping for 30 seconds")
                time.sleep(30)
                continue

            log.debug("Got %d events", len(events))
            for event in events:
                try:
                    node = event.get("node", {})
                    artifacts = node.get("artifacts", {})
                    job_definition_url = artifacts.get("job_definition", "")

                    if not job_definition_url or not job_definition_url.startswith(
                        "http"
                    ):
                        continue

                    group = node.get("group", "")
                    if args.group_filter and args.group_filter not in group:
                        continue

                    data = event.get("data", {}).get("data", {})
                    platform = data.get("platform")
                    runtime = data.get("runtime")

                    if args.platform and platform != args.platform:
                        continue
                    if args.runtime and runtime != args.runtime:
                        continue

                    log.info("Matched job: %s", job_definition_url)
                    jobdata = retrieve_job_definition(job_definition_url)

                    if "kernel" not in jobdata.get("artifacts", {}):
                        log.debug("Skipping - missing kernel artifact")
                        continue

                    if "ramdisk" not in jobdata.get("artifacts", {}):
                        log.debug(
                            "Skipping - no ramdisk artifact (nfsboot kernel requires NFS)"
                        )
                        continue

                    output_dir = None
                    if not args.no_save_outputs and args.output_dir:
                        output_dir = os.path.join(
                            args.output_dir, node.get("id", "unknown")
                        )

                    run_pytest(jobdata, tests_dir=args.tests_dir, output_dir=output_dir)

                except Exception as e:
                    log.error("Error processing event: %s", e)
                    traceback.print_exc()

            if events:
                timestamp = events[-1]["timestamp"]
        except requests.exceptions.RequestException as e:
            retry_count += 1
            log.error(
                "Error fetching events (attempt %d/%d): %s",
                retry_count,
                args.max_retries,
                e,
            )
            if retry_count >= args.max_retries:
                log.error("Max retries (%d) reached. Exiting.", args.max_retries)
                sys.exit(1)
            log.info("Retrying in 30 seconds...")
            time.sleep(30)


def direct_run(args):
    rc = run_pytest(
        args.kci_job_json,
        tests_dir=args.tests_dir,
        output_dir=args.output_dir if not args.no_save_outputs else None,
    )
    sys.exit(rc)


def main():
    parser = argparse.ArgumentParser(
        description="Poll KernelCI API for pull-lab jobs and run them with pytest + QEMU."
    )
    parser.add_argument(
        "--tests-dir",
        default=DEFAULT_TESTS_DIR,
        help="Directory containing pytest tests (default: example_pytest_tests/).",
    )
    parser.add_argument(
        "--output-dir",
        default="./test_output",
        help="Directory for JUnit XML results (default: ./test_output).",
    )
    parser.add_argument(
        "--no-save-outputs", action="store_true", help="Disable saving outputs."
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging.")

    subparsers = parser.add_subparsers(dest="command")

    poll_parser = subparsers.add_parser(
        "poll", help="Poll KernelCI events API (default)."
    )
    poll_parser.add_argument("--platform", default="", help="Filter by platform.")
    poll_parser.add_argument("--runtime", help="Filter by runtime/lab name.")
    poll_parser.add_argument(
        "--group-filter",
        default="pull-labs",
        help="Filter by group (default: pull-labs).",
    )
    poll_parser.add_argument(
        "--max-retries", type=int, default=5, help="Max API retries (default: 5)."
    )

    direct_parser = subparsers.add_parser(
        "direct", help="Run pytest against a local job JSON."
    )
    direct_parser.add_argument(
        "--kci-job-json", required=True, help="Path to job definition JSON."
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "direct":
        direct_run(args)
    else:
        poll_loop(args)


if __name__ == "__main__":
    main()
