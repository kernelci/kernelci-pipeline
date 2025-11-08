#!/usr/bin/env python3
"""
This example will retrieve the latest events from the KernelCI API
and print them to the console.
It will filter only completed kernel builds, limit to 100 events per request,
and retrieve corresponding nodes with artifacts.
"""
import argparse
import tempfile
import requests
import json
import sys
import os
import time
import hashlib
import subprocess


# This is staging server: "https://staging.kernelci.org:9000/latest"
# For production use "https://api.kernelci.org/latest/"
BASE_URI = "https://staging.kernelci.org:9000/latest"
EVENTS_PATH = "/events"

# Start from the beginning of time, but you might implement
# saving last processed timestamp to a file or database
timestamp = "1970-01-01T00:00:00.000000"


"""
kind:

There are a few different kinds:

* checkout: a new git tree checkout to be tested. Maestro frequently cuts
    new tree checkout from tree it is subscribed to. See 'config/pipeline.yaml'
* kbuild: a new kernel build for a given config and arch
* job: the execution of a test suite
* test: the execution of a test inside a job


state:

In this example we track state=done to get an event when Maestro is ready to
provide all the information about the node. Eg for checkout it will provide
the commit hash to test and for builds the location of the kernel binaries built.
"""


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


def check_cache(url):
    # if file exists in _cache/ with name as md5 of url, return path
    md5 = hashlib.md5(url.encode()).hexdigest()
    cache_path = f"_cache/{md5}"
    if os.path.exists(cache_path):
        print(f"Cache hit for {url}")
        return cache_path
    print(f"Cache miss for {url}")
    return None


def store_in_cache(url, filepath):
    # I have slow internet, so i need to cache downloads
    md5 = hashlib.md5(url.encode()).hexdigest()
    cache_path = f"_cache/{md5}"
    os.makedirs("_cache", exist_ok=True)
    os.system(f"cp {filepath} {cache_path}")
    print(f"Stored {url} in cache at {cache_path}")


def download_artifact(url, dest):
    cached_path = check_cache(url)
    if cached_path:
        # If we have a cached version, use it
        print(f"Using cached version of {url}")
        # copy cached file to dest
        os.system(f"cp {cached_path} {dest}")
        return
    print(f"Downloading artifact from: {url} to {dest}")
    response = requests.get(url, stream=True)
    response.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    # Store downloaded file in cache
    store_in_cache(url, dest)


def launch_x86_vm(kernel, ramdisk):
    # This is a placeholder function to launch the x86 VM using QEMU
    # In a real implementation, you would use subprocess to call QEMU with appropriate arguments
    print(f"Launching x86 VM with kernel: {kernel} and ramdisk: {ramdisk}")
    # Example command (not executed here):
    cmd = f"qemu-system-x86_64 -kernel {kernel} -initrd {ramdisk} -m 1024 -nographic -append 'console=ttyS0'"
    print(f"Executing command: {cmd}")
    os.system(cmd)


def prepare_x86(artifacts):
    # create temporary directory for all qemu files
    with tempfile.TemporaryDirectory() as tmpdir:
        kernel_url = artifacts.get("kernel")
        modules_url = artifacts.get("modules")
        ramdisk_url = artifacts.get("ramdisk")
        kernel = f"{tmpdir}/kernel"
        modules = f"{tmpdir}/modules.tar.xz"
        ramdisk = f"{tmpdir}/ramdisk.cpio.gz"
        download_artifact(kernel_url, kernel)
        download_artifact(modules_url, modules)
        download_artifact(ramdisk_url, ramdisk)
        modules_dir = os.path.join(tmpdir, "modules_temp")
        os.makedirs(modules_dir, exist_ok=True)
        subprocess.run(["tar", "-xf", modules, "-C", modules_dir], check=True)

        modules_cpio = os.path.join(tmpdir, "modules.cpio")
        subprocess.run(
            f"(cd {modules_dir} && find . | cpio -o --format=newc) > {modules_cpio}",
            shell=True,
            check=True,
        )

        subprocess.run(
            ["gzip", "-d", "-f", ramdisk], check=True
        )  # produces ramdisk.cpio in same dir
        original_cpio = os.path.join(tmpdir, "ramdisk.cpio")

        merged_cpio = os.path.join(tmpdir, "new_ramdisk.cpio")
        # Simple concatenation preserves all original entries then adds modules entries
        with open(merged_cpio, "wb") as out_f:
            for part in (original_cpio, modules_cpio):
                with open(part, "rb") as in_f:
                    out_f.write(in_f.read())

        # 5. Compress merged archive
        subprocess.run(["gzip", "-f", merged_cpio], check=True)
        ramdisk = f"{merged_cpio}.gz"
        print(
            f"Launching x86 VM with kernel: {kernel}, modules: {modules}, ramdisk: {ramdisk}"
        )
        print("To quit the VM, use Ctrl-A X in the QEMU window.")
        print("Press Enter to continue...")
        input()
        launch_x86_vm(kernel, ramdisk)


def main():
    global timestamp

    parser = argparse.ArgumentParser(description="Listen to events in Maestro.")
    args = parser.parse_args()
    while True:
        try:
            events = pollevents(timestamp, "job")
            if len(events) == 0:
                print("No new events, sleeping for 30 seconds")
                time.sleep(30)
                continue
            print(f"Got {len(events)} events")
            for event in events:
                data = event.get("data", {}).get("data", {})
                if (
                    data.get("platform") == "qemu"
                    and data.get("runtime", {}) == "pull-labs-demo"
                ):
                    # print(json.dumps(event, indent=2))
                    # retrieve data.node.artifacts.job_definition
                    node = event.get("node", {})
                    artifacts = node.get("artifacts", {})
                    job_definition_url = artifacts.get("job_definition", "")
                    # validate if job_definition is valid url
                    if not job_definition_url.startswith("http"):
                        print(f"Invalid job_definition URL: {job_definition_url}")
                        continue
                    print(f"Valid job_definition URL: {job_definition_url}")
                    jobdata = retrieve_job_definition(job_definition_url)
                    # we must have inside artifacts and inside artifacts kernel,modules,ramdisk
                    job_artifacts = jobdata.get("artifacts", {})
                    if all(
                        key in job_artifacts for key in ["kernel", "modules", "ramdisk"]
                    ):
                        print("Job definition contains all required artifacts")
                        prepare_x86(job_artifacts)

                # print(json.dumps(data2, indent=2))
                # print(json.dumps(event, indent=2))
                timestamp = event["timestamp"]
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
