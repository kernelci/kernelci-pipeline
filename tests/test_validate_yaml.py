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


def fragment_config(fragments=None, rule=None):
    entry = lava_entry("ltp-syscalls", ["bcm2711-rpi-4-b"])
    entry["event"]["name"] = "kbuild-arm64"
    data = config(["ltp-syscalls", "kbuild-arm64"], [entry])
    data["jobs"]["kbuild-arm64"] = {
        "kind": "kbuild",
        "params": {"fragments": fragments},
    }
    data["jobs"]["ltp-syscalls"]["rules"] = {
        "fragments": rule if rule is not None else ["!kselftest"]
    }
    data["runtimes"] = {"lava-broonie": {"lab_type": "lava"}}
    return data


def test_fragment_conflict_reports_issue_1226():
    data = fragment_config(["lab-setup", "kselftest"])
    assert validate_yaml.find_blocked_schedules(data) == [
        "Job ltp-syscalls has no compatible input builds on "
        "bcm2711-rpi-4-b (lava-broonie). Rejected builds: kbuild-arm64: "
        "job ltp-syscalls rules[fragments] rejects ['kselftest']"
    ]


@pytest.mark.parametrize(
    "fragments, rule, reason",
    [
        (["ltp", "nfs-root-boot"], ["!kselftest"], None),
        (["ltp"], ["ltp", "kselftest"], None),
        (["ltp", "kselftest"], ["ltp", "!kselftest"], "rejects"),
        (["ltp"], ["kselftest"], "requires one of ['kselftest']"),
        ([], ["kselftest"], "requires one of ['kselftest']"),
        ([], ["!kselftest"], None),
        (["ltp"], [], None),
        (None, ["kselftest"], None),
        ("{fragments}", ["!kselftest"], None),
        (["{fragment}"], ["kselftest"], None),
        (["ltp"], ["{required_fragment}"], None),
    ],
)
def test_fragment_rules_match_allow_and_deny_semantics(fragments, rule, reason):
    warnings = validate_yaml.find_blocked_schedules(
        fragment_config(fragments, rule)
    )
    if reason:
        assert len(warnings) == 1
        assert reason in warnings[0]
    else:
        assert warnings == []


@pytest.mark.parametrize("source", ["scheduler", "job", "platform", "runtime"])
def test_fragment_rules_at_each_scope(source):
    data = fragment_config(["kselftest"], [])
    scopes = {
        "scheduler": data["scheduler"][0],
        "job": data["jobs"]["ltp-syscalls"],
        "platform": data["platforms"]["bcm2711-rpi-4-b"],
        "runtime": data["runtimes"]["lava-broonie"],
    }
    scopes[source]["rules"] = {"fragments": ["!kselftest"]}
    warnings = validate_yaml.find_blocked_schedules(data)
    assert len(warnings) == 1
    assert source in warnings[0].split("Rejected builds: ")[1]


def test_platform_fragment_rules_only_warn_for_affected_target():
    data = fragment_config(["kselftest"], [])
    data["scheduler"][0]["platforms"].append("other-board")
    data["platforms"]["other-board"] = {}
    data["platforms"]["bcm2711-rpi-4-b"]["rules"] = {
        "fragments": ["!kselftest"]
    }
    warnings = validate_yaml.find_blocked_schedules(data)
    assert len(warnings) == 1
    assert "bcm2711-rpi-4-b" in warnings[0]
    assert "other-board" not in warnings[0]


@pytest.mark.parametrize(
    "event",
    [
        {"channel": "node", "kind": "kbuild", "name": "external-build"},
        {"channel": "node", "kind": "job", "name": "kbuild-arm64"},
        {"channel": "other", "kind": "kbuild", "name": "kbuild-arm64"},
    ],
)
def test_unknown_or_non_build_inputs_are_not_guessed(event):
    data = fragment_config(["kselftest"])
    data["scheduler"][0]["event"] = event
    assert validate_yaml.find_blocked_schedules(data) == []


def test_compatible_alternative_prevents_zero_match_warning():
    data = fragment_config(["kselftest"])
    data["jobs"]["kbuild-ltp"] = {
        "kind": "kbuild",
        "params": {"fragments": ["ltp"]},
    }
    entry = lava_entry("ltp-syscalls", ["bcm2711-rpi-4-b"])
    entry["event"]["name"] = "kbuild-ltp"
    data["scheduler"].append(entry)
    warnings = validate_yaml.find_blocked_schedules(data)
    assert warnings == []


def test_merged_yaml_aliases_are_checked(tmp_path):
    (tmp_path / "jobs.yaml").write_text("""
jobs:
  kbuild-arm64:
    kind: kbuild
    params:
      fragments: [lab-setup, kselftest]
  ltp-template: &ltp
    rules:
      fragments: ['!kselftest']
  ltp-syscalls:
    <<: *ltp
""")
    (tmp_path / "scheduler.yaml").write_text("""
scheduler:
  - job: ltp-syscalls
    event: {channel: node, kind: kbuild, name: kbuild-arm64}
    runtime: {name: lava-broonie}
    platforms: [bcm2711-rpi-4-b]
""")
    data = validate_yaml.merge_files(str(tmp_path))
    warnings = validate_yaml.find_blocked_schedules(data)
    assert len(warnings) == 1
    assert "rejects ['kselftest']" in warnings[0]


@pytest.mark.parametrize(
    "github_actions, prefix",
    [
        ("true", "::warning title=No compatible input builds::"),
        ("", "Warning: "),
    ],
)
def test_report_fragment_conflicts_output(
    monkeypatch, capsys, github_actions, prefix
):
    monkeypatch.setenv("GITHUB_ACTIONS", github_actions)
    validate_yaml.report_blocked_schedules(fragment_config(["kselftest"]))
    assert (
        f"{prefix}Job ltp-syscalls has no compatible input builds"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "other_fragments, blocked",
    [(["kselftest"], True), (["ltp"], False), (None, False)],
)
def test_unnamed_build_event_checks_all_candidates(other_fragments, blocked):
    data = fragment_config(["kselftest"])
    del data["scheduler"][0]["event"]["name"]
    data["jobs"]["other-build"] = {
        "kind": "kbuild",
        "params": {"fragments": other_fragments},
    }
    warnings = validate_yaml.find_blocked_schedules(data)
    assert bool(warnings) == blocked
    if blocked:
        assert "kbuild-arm64" in warnings[0]
        assert "other-build" in warnings[0]


def test_unknown_alternative_input_prevents_zero_match_warning():
    data = fragment_config(["kselftest"])
    entry = lava_entry("ltp-syscalls", ["bcm2711-rpi-4-b"])
    entry["event"].update(kind="job", name="baseline")
    data["scheduler"].append(entry)
    assert validate_yaml.find_blocked_schedules(data) == []


def test_type_selected_runtime_alternative_prevents_zero_match_warning():
    data = fragment_config(["kselftest"])
    data["jobs"]["kbuild-ltp"] = {
        "kind": "kbuild",
        "params": {"fragments": ["ltp"]},
    }
    entry = lava_entry("ltp-syscalls", ["bcm2711-rpi-4-b"])
    entry["event"]["name"] = "kbuild-ltp"
    entry["runtime"] = {"type": "lava"}
    data["scheduler"].append(entry)
    assert validate_yaml.find_blocked_schedules(data) == []


def test_alternative_for_other_platform_does_not_hide_zero_matches():
    data = fragment_config(["kselftest"])
    data["jobs"]["kbuild-ltp"] = {
        "kind": "kbuild",
        "params": {"fragments": ["ltp"]},
    }
    entry = lava_entry("ltp-syscalls", ["other-board"])
    entry["event"]["name"] = "kbuild-ltp"
    data["scheduler"].append(entry)
    warnings = validate_yaml.find_blocked_schedules(data)
    assert len(warnings) == 1
    assert "bcm2711-rpi-4-b" in warnings[0]


@pytest.mark.parametrize("field", ["arch", "compiler", "defconfig"])
def test_fixed_build_fields_can_prove_no_matches(field):
    data = fragment_config(["ltp"], [])
    data["jobs"]["kbuild-arm64"]["params"][field] = "actual"
    data["jobs"]["ltp-syscalls"]["rules"][field] = ["required"]
    warnings = validate_yaml.find_blocked_schedules(data)
    assert len(warnings) == 1
    assert f"rules[{field}] requires one of ['required']" in warnings[0]


def test_build_variant_list_is_not_treated_as_single_build():
    data = fragment_config(["ltp"], [])
    data["jobs"]["kbuild-arm64"]["params"]["defconfig"] = [
        "defconfig",
        "allnoconfig",
    ]
    data["jobs"]["ltp-syscalls"]["rules"]["defconfig"] = ["!allnoconfig"]
    assert validate_yaml.find_blocked_schedules(data) == []


def test_runtime_dependent_rules_do_not_use_a_synthetic_checkout():
    data = fragment_config(["ltp"], [])
    data["jobs"]["ltp-syscalls"]["rules"].update(
        tree=["!mainline"],
        branch=["!master"],
        min_version={"version": 99, "patchlevel": 0},
    )
    assert validate_yaml.find_blocked_schedules(data) == []


def test_multiple_rules_may_reject_different_candidate_builds():
    data = fragment_config(["kselftest"])
    del data["scheduler"][0]["event"]["name"]
    data["jobs"]["kbuild-x86"] = {
        "kind": "kbuild",
        "params": {"arch": "x86_64", "fragments": ["ltp"]},
    }
    data["platforms"]["bcm2711-rpi-4-b"]["rules"] = {"arch": ["arm64"]}
    warnings = validate_yaml.find_blocked_schedules(data)
    assert len(warnings) == 1
    assert "rules[fragments] rejects ['kselftest']" in warnings[0]
    assert "rules[arch] requires one of ['arm64']" in warnings[0]


def test_zero_match_warning_is_part_of_normal_validation(monkeypatch, capsys):
    for name in (
        "validate_jobs",
        "validate_scheduler_jobs",
        "validate_unused_jobs",
        "validate_build_configs",
        "validate_unused_trees",
        "validate_platforms",
        "validate_fragments",
        "validate_rootfs_refs",
    ):
        monkeypatch.setattr(validate_yaml, name, lambda data: None)
    validate_yaml.validate_yaml(fragment_config(["kselftest"]))
    assert "has no compatible input builds" in capsys.readouterr().out
