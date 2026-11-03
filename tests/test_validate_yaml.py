import os
import subprocess

import pytest
import yaml

import validate_yaml


def lava_entry(job, platforms, lab="lava-broonie"):
    return {
        "job": job,
        "event": {"channel": "node", "kind": "kbuild", "state": "done"},
        "runtime": {"type": "lava", "name": lab},
        "platforms": platforms,
    }


def config(jobs, scheduler):
    platforms = {
        platform: {}
        for entry in scheduler
        for platform in entry.get("platforms") or []
    }
    return {
        "jobs": {job: {"kind": "job"} for job in jobs},
        "scheduler": scheduler,
        "platforms": platforms,
    }


def test_scheduled_targets_without_platforms_uses_runtime_type():
    data = config(
        ["kbuild"],
        [{"job": "kbuild", "runtime": {"type": "kubernetes"}}],
    )
    assert validate_yaml.scheduled_targets(data) == {
        "kbuild": {("kubernetes", None)}
    }


def test_no_change_reports_nothing():
    data = config(["ltp-ipc"], [lava_entry("ltp-ipc", ["beaglebone-black"])])
    assert validate_yaml.find_dropped_schedules(data, data) == []


def test_platform_dropped_while_job_still_scheduled_elsewhere():
    base = config(
        ["ltp-ipc"],
        [
            lava_entry("ltp-ipc", ["beaglebone-black", "bcm2711-rpi-4-b"]),
            lava_entry("ltp-ipc", ["volteer"], lab="lava-collabora"),
        ],
    )
    head = config(
        ["ltp-ipc"],
        [lava_entry("ltp-ipc", ["volteer"], lab="lava-collabora")],
    )
    head["platforms"] = base["platforms"]
    assert validate_yaml.find_dropped_schedules(base, head) == [
        "Job ltp-ipc is still defined but no longer scheduled on: "
        "bcm2711-rpi-4-b (lava-broonie), beaglebone-black (lava-broonie)"
    ]


def test_job_dropped_from_every_scheduler_entry():
    base = config(["ltp-fsx"], [lava_entry("ltp-fsx", ["beaglebone-black"])])
    head = config(["ltp-fsx"], [])
    head["platforms"] = base["platforms"]
    assert validate_yaml.find_dropped_schedules(base, head) == [
        "Job ltp-fsx is still defined but no longer scheduled anywhere "
        "(was scheduled on: beaglebone-black (lava-broonie))"
    ]


def test_job_moved_to_another_lab_is_reported():
    base = config(["ltp-mm"], [lava_entry("ltp-mm", ["beaglebone-black"])])
    head = config(
        ["ltp-mm"],
        [lava_entry("ltp-mm", ["beaglebone-black"], lab="lava-baylibre")],
    )
    assert validate_yaml.find_dropped_schedules(base, head) == [
        "Job ltp-mm is still defined but no longer scheduled on: "
        "beaglebone-black (lava-broonie)"
    ]


def test_job_removed_together_with_its_definition_is_not_reported():
    base = config(["ltp-ipc"], [lava_entry("ltp-ipc", ["beaglebone-black"])])
    head = config([], [])
    assert validate_yaml.find_dropped_schedules(base, head) == []


def test_job_never_scheduled_in_base_is_not_reported():
    base = config(["kunit"], [])
    head = config(["kunit"], [])
    assert validate_yaml.find_dropped_schedules(base, head) == []


def git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
    )


def write_config(repo, data):
    path = repo / "config" / "scheduler.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(yaml.safe_dump(data))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "test")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "commit.gpgsign", "false")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_config_at_ref_reads_committed_config(repo):
    base = config(["ltp-ipc"], [lava_entry("ltp-ipc", ["beaglebone-black"])])
    write_config(repo, base)
    git(repo, "add", "config")
    git(repo, "commit", "-q", "-m", "base")
    write_config(repo, config(["ltp-ipc"], []))

    assert validate_yaml.load_config_at_ref("HEAD") == base
    assert os.path.exists(repo / "config" / "scheduler.yaml")


def test_load_config_at_ref_without_yaml_files_fails(repo):
    (repo / "config").mkdir()
    (repo / "config" / "README").write_text("x")
    git(repo, "add", "config")
    git(repo, "commit", "-q", "-m", "init")

    with pytest.raises(ValueError):
        validate_yaml.load_config_at_ref("HEAD")


def test_dropped_platformless_target_names_the_runtime():
    base = config(
        ["kbuild"],
        [{"job": "kbuild", "runtime": {"name": "k8s-all"}}],
    )
    head = config(["kbuild"], [])
    assert validate_yaml.find_dropped_schedules(base, head) == [
        "Job kbuild is still defined but no longer scheduled anywhere "
        "(was scheduled on: k8s-all)"
    ]


def test_platform_removed_from_platforms_is_not_reported():
    base = config(
        ["baseline"],
        [lava_entry("baseline", ["kevin", "beaglebone-black"])],
    )
    base["platforms"] = {"kevin": {}, "beaglebone-black": {}}
    head = config(["baseline"], [lava_entry("baseline", ["beaglebone-black"])])
    head["platforms"] = {"beaglebone-black": {}}
    assert validate_yaml.find_dropped_schedules(base, head) == []


@pytest.mark.parametrize(
    "github_actions, prefix",
    [
        ("true", "::warning title=Job dropped from scheduler::"),
        ("", "Warning: "),
    ],
)
def test_report_dropped_schedules_output(
    monkeypatch, capsys, github_actions, prefix
):
    base = config(["ltp-fsx"], [lava_entry("ltp-fsx", ["beaglebone-black"])])
    monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    monkeypatch.setattr(
        validate_yaml, "load_config_at_ref", lambda ref, dir: base
    )

    head = config(["ltp-fsx"], [])
    head["platforms"] = base["platforms"]

    validate_yaml.report_dropped_schedules("HEAD", head, "config")

    assert (
        f"{prefix}Job ltp-fsx is still defined but no longer scheduled "
        "anywhere (was scheduled on: beaglebone-black (lava-broonie))"
    ) in capsys.readouterr().out.splitlines()


def test_load_config_at_ref_from_another_directory(repo, tmp_path_factory):
    base = config(["ltp-ipc"], [lava_entry("ltp-ipc", ["beaglebone-black"])])
    write_config(repo, base)
    trees = repo / "config" / "trees"
    trees.mkdir()
    (trees / "mainline.yaml").write_text(
        yaml.safe_dump({"trees": {"mainline": {"url": "x"}}})
    )
    (repo / "tests").mkdir()
    git(repo, "add", "config")
    git(repo, "commit", "-q", "-m", "base")
    os.chdir(repo / "tests")
    tmp = tmp_path_factory.mktemp("tmp")
    os.environ["TMPDIR"] = str(tmp)

    try:
        data = validate_yaml.load_config_at_ref("HEAD", "../config")
    finally:
        del os.environ["TMPDIR"]

    assert data["scheduler"] == base["scheduler"]
    assert data["platforms"] == base["platforms"]
    assert data["trees"] == {"mainline": {"url": "x"}}
    assert os.listdir(tmp) == []
