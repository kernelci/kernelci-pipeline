#!/usr/bin/env python3
"""
Poll KernelCI API for pull-lab jobs and execute them with tuxrun.

Filters for jobs with job_definition artifacts, auto-detects architecture,
and passes kernel/module/rootfs URLs directly to tuxrun for execution.
"""
import argparse
import requests
import sys
import os
import time
import subprocess
import shlex


BASE_URI = "https://staging.kernelci.org:9000/latest"
EVENTS_PATH = "/events"
timestamp = "1970-01-01T00:00:00.000000"


def pollevents(timestamp, kind):
    url = (
        BASE_URI
        + EVENTS_PATH
        + f"?state=done&kind={kind}&limit=1000&recursive=true&from={timestamp}"
    )
    print(url)
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def retrieve_job_definition(url):
    print(f"Retrieving job definition from: {url}")
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def run_tuxrun(kernel_url, modules_url, device="qemu-x86_64", tests=None, rootfs_url=None, cache_dir=None):
    """
    Launch a test using tuxrun

    Args:
        kernel_url: URL to kernel image
        modules_url: URL to modules tarball
        device: tuxrun device type (default: qemu-x86_64)
        tests: Optional test suite to run
        rootfs_url: Optional URL to custom rootfs image
        cache_dir: Optional directory to save tuxrun outputs and cache
    """
    print(f"Running tuxrun with kernel: {kernel_url}, modules: {modules_url}, device: {device}")

    cmd = [
        "tuxrun",
        "--device", device,
        "--kernel", kernel_url,
        "--modules", modules_url,
    ]

    if rootfs_url:
        cmd.extend(["--rootfs", rootfs_url])

    if tests:
        cmd.extend(["--tests", tests])

    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        cmd.extend(["--save-outputs", "--cache-dir", cache_dir])
        print(f"Outputs will be saved to: {cache_dir}")

    print(f"Executing command: {' '.join(shlex.quote(arg) for arg in cmd)}")

    try:
        result = subprocess.run(cmd, check=True)
        print(f"\n✓ tuxrun completed successfully")
        if cache_dir:
            print(f"✓ Outputs saved to: {cache_dir}")
        return result.returncode
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"\n✗ Error running tuxrun: {e}")
        return e.returncode


def prepare_and_run(artifacts, device="qemu-x86_64", tests=None, rootfs_override=None, cache_dir=None):
    """
    Run tuxrun with artifact URLs

    Args:
        artifacts: Dictionary containing URLs for kernel, modules, and optionally rootfs/ramdisk
        device: tuxrun device type (default: qemu-x86_64)
        tests: Optional test suite to run
        rootfs_override: Optional rootfs URL to override the one from artifacts
        cache_dir: Optional directory to save tuxrun outputs and cache
    """
    kernel_url = artifacts.get("kernel")
    modules_url = artifacts.get("modules")
    # Try rootfs first, then ramdisk as fallback
    rootfs_url = rootfs_override if rootfs_override else (artifacts.get("rootfs") or artifacts.get("ramdisk"))

    if not kernel_url or not modules_url:
        print("Missing required artifacts (kernel or modules)")
        return

    print(f"Kernel URL: {kernel_url}")
    print(f"Modules URL: {modules_url}")
    if rootfs_url:
        if rootfs_override:
            print(f"Rootfs URL (override): {rootfs_url}")
        else:
            print(f"Rootfs URL: {rootfs_url}")
    print("Press Enter to launch tuxrun...")
    input()

    return run_tuxrun(kernel_url, modules_url, device=device, tests=tests, rootfs_url=rootfs_url, cache_dir=cache_dir)


def main():
    global timestamp

    parser = argparse.ArgumentParser(
        description="Listen to events in Maestro and run jobs with tuxrun."
    )
    parser.add_argument(
        "--device",
        default=None,
        help="Filter jobs by device type (e.g., qemu-x86_64, qemu-arm64). If not specified, accepts all architectures.",
    )
    parser.add_argument(
        "--tests",
        help="Optional test suite to run with tuxrun",
    )
    parser.add_argument(
        "--rootfs",
        help="Optional rootfs URL to use with tuxrun",
    )
    parser.add_argument(
        "--cache-dir",
        help="Directory to save tuxrun outputs and cache (e.g., ./outputs). Enables --save-outputs flag.",
    )
    parser.add_argument(
        "--platform",
        default="qemu",
        help="Filter jobs by platform (default: qemu). Use empty string to accept all platforms.",
    )
    parser.add_argument(
        "--runtime",
        help="Filter jobs by runtime/lab name (e.g., pull-labs-demo). If not specified, accepts all runtimes.",
    )
    parser.add_argument(
        "--group-filter",
        default="pull-labs",
        help="Filter jobs by group name containing this string (default: pull-labs). Use empty string to accept all groups.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum number of consecutive retries on API errors (default: 5)",
    )
    args = parser.parse_args()

    retry_count = 0
    while True:
        try:
            events = pollevents(timestamp, "job")
            retry_count = 0

            if len(events) == 0:
                print("No new events, sleeping for 30 seconds")
                time.sleep(30)
                continue
            print(f"Got {len(events)} events")
            for event in events:
                try:
                    node = event.get("node", {})
                    artifacts = node.get("artifacts", {})
                    job_definition_url = artifacts.get("job_definition", "")

                    if not job_definition_url:
                        continue

                    if not job_definition_url.startswith("http"):
                        print(f"Invalid job_definition URL: {job_definition_url}")
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

                    print(f"Valid job_definition URL: {job_definition_url}")
                    print(f"Job group: {group}")
                    if platform:
                        print(f"Platform: {platform}")
                    if runtime:
                        print(f"Runtime: {runtime}")

                    jobdata = retrieve_job_definition(job_definition_url)
                    job_artifacts = jobdata.get("artifacts", {})
                    if all(key in job_artifacts for key in ["kernel", "modules"]):
                        print("Job definition contains required artifacts for tuxrun")

                        job_tests = jobdata.get("tests", [])
                        if job_tests:
                            print(f"Job defines {len(job_tests)} test(s):")
                            for test in job_tests:
                                test_type = test.get("type", "unknown")
                                test_id = test.get("id", "unknown")
                                print(f"  - {test_id} (type: {test_type})")

                        environment = jobdata.get("environment", {})
                        arch = environment.get("arch", "x86_64")
                        platform = environment.get("platform", "")

                        if not platform:
                            print(f"Skipping job - no platform specified in environment")
                            continue

                        # Use platform as device, or construct it if platform is generic
                        if platform.startswith("qemu-"):
                            device = platform
                        elif platform == "qemu":
                            # Fallback: construct device from platform + arch
                            device = f"qemu-{arch}"
                        else:
                            device = platform

                        print(f"Job environment: platform={platform}, arch={arch} -> device={device}")

                        if args.device and device != args.device:
                            print(f"Skipping job - device {device} does not match filter {args.device}")
                            continue

                        cache_dir = None
                        if args.cache_dir:
                            node_id = node.get("id", "unknown")
                            cache_dir = os.path.join(args.cache_dir, node_id)

                        prepare_and_run(
                            job_artifacts,
                            device=device,
                            tests=args.tests,
                            rootfs_override=args.rootfs,
                            cache_dir=cache_dir
                        )
                    else:
                        print(
                            "Skipping job - missing required artifacts (kernel or modules)"
                        )

                except Exception as e:
                    print(f"Error processing event: {e}")
                    import traceback
                    traceback.print_exc()

            if events:
                timestamp = events[-1]["timestamp"]
        except requests.exceptions.RequestException as e:
            retry_count += 1
            print(f"Error fetching events (attempt {retry_count}/{args.max_retries}): {e}")
            if retry_count >= args.max_retries:
                print(f"Max retries ({args.max_retries}) reached. Exiting.")
                sys.exit(1)
            print("Retrying in 30 seconds...")
            time.sleep(30)


if __name__ == "__main__":
    main()
