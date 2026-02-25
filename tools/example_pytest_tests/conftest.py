import json
import logging
import shutil

import pytest
import requests
import yaml
from labgrid import Environment

QEMU_CONFIGS = {
    "x86_64": ("qemu-system-x86_64", "pc", "max", "ttyS0"),
    "arm64": ("qemu-system-aarch64", "virt", "cortex-a57", "ttyAMA0"),
    "arm": ("qemu-system-arm", "virt", "cortex-a15", "ttyAMA0"),
}

REQUEST_TIMEOUT = 120

log = logging.getLogger(__name__)


def pytest_addoption(parser):
    parser.addoption(
        "--kci-job-json",
        required=True,
        help="Path to KernelCI job definition JSON.",
    )


def _download(url, path):
    log.debug("Downloading %s", url)
    resp = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    with open(path, "wb") as f:
        for chunk in resp.iter_content(8192):
            f.write(chunk)


def _write_env_yaml(path, qemu_bin, machine, cpu, console, kernel, ramdisk=None):
    extra_args = "-no-reboot"
    if ramdisk:
        extra_args += f" -initrd {ramdisk}"

    config = {
        "targets": {
            "main": {
                "drivers": {
                    "QEMUDriver": {
                        "qemu_bin": "qemu",
                        "machine": machine,
                        "cpu": cpu,
                        "memory": "512M",
                        "boot_args": f"console={console}",
                        "kernel": "kernel",
                        "extra_args": extra_args,
                    },
                    "ShellDriver": {
                        "prompt": ".*[#$] ",
                        "login_prompt": "login:",
                        "username": "root",
                    },
                },
            },
        },
        "tools": {"qemu": qemu_bin},
        "images": {"kernel": kernel},
    }

    with open(path, "w") as f:
        yaml.dump(config, f)


@pytest.fixture(scope="session")
def kci_job(request):
    with open(request.config.getoption("--kci-job-json")) as f:
        return json.load(f)


@pytest.fixture(scope="session")
def kci_environment(kci_job):
    return kci_job.get("environment", {})


@pytest.fixture(scope="session")
def shell(kci_job, kci_environment, tmp_path_factory):
    tmpdir = tmp_path_factory.mktemp("qemu")
    arch = kci_environment.get("arch", "x86_64")
    qemu_name, machine, cpu, console = QEMU_CONFIGS.get(arch, QEMU_CONFIGS["x86_64"])

    qemu_bin = shutil.which(qemu_name)
    if not qemu_bin:
        pytest.skip(f"QEMU binary not found: {qemu_name}")

    artifacts = kci_job["artifacts"]
    kernel = str(tmpdir / "kernel")
    _download(artifacts["kernel"], kernel)

    ramdisk_url = artifacts.get("ramdisk")
    if not ramdisk_url:
        pytest.skip(
            "No ramdisk artifact - nfsboot kernels require NFS and cannot run in QEMU with -initrd"
        )

    ramdisk = str(tmpdir / "ramdisk")
    _download(ramdisk_url, ramdisk)

    env_yaml = str(tmpdir / "env.yaml")
    _write_env_yaml(env_yaml, qemu_bin, machine, cpu, console, kernel, ramdisk)
    log.debug("Labgrid env: %s", env_yaml)

    env = Environment(env_yaml)
    target = env.get_target("main")
    qemu = target.get_driver("QEMUDriver")
    qemu.on()
    shell_driver = target.get_driver("ShellDriver")

    yield shell_driver

    target.cleanup()
