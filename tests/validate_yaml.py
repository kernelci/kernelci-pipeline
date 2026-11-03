#!/usr/bin/env python3
"""
Validate all yaml files in the config/ directory
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

import yaml


class NoAliasDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def recursive_merge(d1, d2, detect_same_keys=False):
    """
    Recursively merge two dictionaries, which might have lists
    Not sure if i done this right, but it works
    """
    for k, v in d2.items():
        if detect_same_keys and k in d1:
            if d1[k] != v:
                raise ValueError(
                    f"Key {k} has different values in both dictionaries"
                )
        # We have entries duplication in the yaml files, we need to deal with it later
        # so previous verification is very important
        #            else:
        #                print(f"Warning: Key {k} has same values in both dictionaries")
        if k in d1:
            if isinstance(v, dict):
                d1[k] = recursive_merge(d1[k], v, detect_same_keys=True)
            elif isinstance(v, list):
                d1[k] += v
            else:
                d1[k] = v
        else:
            d1[k] = v
    return d1


def validate_jobs(jobs):
    """
    Validate jobs, they must have a kcidb_test_suite mapping
    """
    for name, definition in jobs.items():
        if not definition.get("kind"):
            raise yaml.YAMLError(f"Kind not found for job: {name}'")
        if definition.get("kind") in ("test", "job"):
            if not definition.get("kcidb_test_suite"):
                raise yaml.YAMLError(
                    f"KCIDB test suite mapping not found for job: {name}'"
                )
        if definition.get("kind") == "job":
            if not definition.get("template"):
                raise yaml.YAMLError(f"Template not found for job: {name}'")
            template = definition.get("template")
            if template == "generic.jinja2":
                # test_method in params
                if not definition.get("params"):
                    raise yaml.YAMLError(f"Params not found for job: {name}'")
                params = definition.get("params")
                if not params.get("test_method"):
                    raise yaml.YAMLError(
                        f"Test method not found for job: {name}'"
                    )
                if params.get("fragments"):
                    raise yaml.YAMLError(
                        f"Fragments not allowed in jobs: {name}'"
                    )


def validate_scheduler_jobs(data):
    """
    Each entry in scheduler have a job, that should be defined in jobs
    """
    schedules = data.get("scheduler")
    jobs = data.get("jobs")
    runtimes = data.get("runtimes")
    for entry in schedules:
        jobname = entry.get("job")
        if entry.get("job"):
            if not entry.get("runtime"):
                raise yaml.YAMLError(f"'runtime' not found in {jobname}")
        if jobname not in jobs.keys():
            raise yaml.YAMLError(f"Job {jobname} not found in jobs")
        jobinfo = jobs[jobname]
        # scheduler entry must have defined in event: channel, (state or result), kind
        event = entry.get("event")
        if not event:
            raise yaml.YAMLError(
                f"Event not found for scheduler entry: {jobname}: {entry}"
            )
        if not event.get("channel"):
            raise yaml.YAMLError(
                f"Channel not found for event: {jobname}: {entry}"
            )
        if not event.get("state"):
            raise yaml.YAMLError(
                f"State not found for event: {jobname}: {entry}"
            )
        if not event.get("kind"):
            raise yaml.YAMLError(
                f"Kind not found for event: {jobname}: {entry}"
            )
        # if we have parameter: platforms, we need to make sure it exists in config
        if entry.get("platforms"):
            for platform in entry.get("platforms"):
                if platform not in data.get("platforms"):
                    raise yaml.YAMLError(
                        f"Platform {platform} not found in platforms"
                    )
        if jobinfo.get("kind") == "kbuild":
            if entry.get("platforms"):
                # kbuild jobs should not have platforms defined in scheduler
                raise yaml.YAMLError(
                    f"Platform not allowed for kbuild job {jobname} in scheduler entry: {entry}"
                )
            runtime = entry.get("runtime")
            if not runtime:
                raise yaml.YAMLError(
                    f"Runtime not found for kbuild job {jobname} in scheduler entry: {entry}"
                )
            runtimename = runtime.get("name")
            if not runtimename:
                raise yaml.YAMLError(
                    f"Runtime name not found for kbuild job {jobname} in scheduler entry: {entry}"
                )
            runtimeinfo = runtimes.get(runtimename)
            if not runtimeinfo:
                raise yaml.YAMLError(
                    f"Runtime {runtimename} not found in runtimes for "
                    f"kbuild job {jobname} in scheduler entry: {entry}"
                )
            lab_type = runtimeinfo.get("lab_type")
            if not lab_type:
                raise yaml.YAMLError(
                    f"Lab type not found for runtime {runtimename} in scheduler entry: {jobname}: {entry}"
                )
            if lab_type not in ("kubernetes", "docker"):
                raise yaml.YAMLError(
                    f"Lab type {lab_type} not allowed for kbuild job {jobname} in scheduler entry: {entry}"
                )


def validate_unused_jobs(data):
    """
    Check if all jobs are used in scheduler
    """
    schedules = data.get("scheduler")
    jobs = data.get("jobs")
    sch_jobs = [entry.get("job") for entry in schedules]
    for job in jobs.keys():
        if job not in sch_jobs:
            print(f"Warning: Job {job} is not used in scheduler")


def scheduled_targets(data):
    """
    Map each scheduled job to the set of (runtime, platform) pairs it
    is scheduled on. Entries without platforms use None as platform.
    """
    targets = {}
    for entry in data.get("scheduler") or []:
        runtime = entry.get("runtime") or {}
        runtime_name = runtime.get("name") or runtime.get("type")
        for platform in entry.get("platforms") or [None]:
            targets.setdefault(entry.get("job"), set()).add(
                (runtime_name, platform)
            )
    return targets


def _format_target(target):
    runtime, platform = target
    if platform is None:
        return str(runtime)
    return f"{platform} ({runtime})"


def find_dropped_schedules(base_data, data):
    """
    Compare scheduler entries against a base config and return a
    warning for every job that is still defined but has lost some or
    all of the runtimes and platforms it was scheduled on in the base.
    Jobs removed from jobs: altogether, and platforms removed from
    platforms: altogether, are not reported.
    """
    base_targets = scheduled_targets(base_data)
    targets = scheduled_targets(data)
    jobs = data.get("jobs") or {}
    platforms = data.get("platforms") or {}
    warnings = []
    for job in sorted(base_targets):
        if job not in jobs:
            continue
        dropped = {
            (runtime, platform)
            for runtime, platform in base_targets[job] - targets.get(job, set())
            if platform is None or platform in platforms
        }
        if not dropped:
            continue
        dropped = ", ".join(
            _format_target(target)
            for target in sorted(
                dropped, key=lambda t: (str(t[0]), str(t[1] or ""))
            )
        )
        if job in targets:
            warnings.append(
                f"Job {job} is still defined but no longer scheduled "
                f"on: {dropped}"
            )
        else:
            warnings.append(
                f"Job {job} is still defined but no longer scheduled "
                f"anywhere (was scheduled on: {dropped})"
            )
    return warnings


def load_config_at_ref(ref, dir="config"):
    """
    Merge the yaml files of the config directory as they are at the
    given git ref, without touching the working tree.
    """
    listing = subprocess.run(
        [
            "git",
            "-C",
            dir,
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            ref,
            "--",
            ".",
        ],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout.decode()
    paths = [path for path in listing.split("\0") if path.endswith(".yaml")]
    if not paths:
        raise ValueError(f"No yaml files found in {dir} at {ref}")
    with tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            content = subprocess.run(
                ["git", "-C", dir, "show", f"{ref}:./{path}"],
                check=True,
                stdout=subprocess.PIPE,
            ).stdout
            dest = os.path.join(tmp, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(content)
        return merge_files(tmp)


def report_dropped_schedules(base_ref, merged_data, dir="config"):
    print(f"Checking for jobs dropped from the scheduler since {base_ref}")
    base_data = load_config_at_ref(base_ref, dir)
    for message in find_dropped_schedules(base_data, merged_data):
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=Job dropped from scheduler::{message}")
        else:
            print(f"Warning: {message}")


def validate_build_configs(data):
    """
    Each entry in build_configs must have a tree and branch attribute
    When referenced, a given tree must exist in the trees: section
    """
    build_configs = data.get("build_configs")
    trees = data.get("trees")
    for entry in build_configs:
        tree = build_configs[entry].get("tree")
        if not tree:
            raise yaml.YAMLError(f"Tree not found for build config: {entry}'")
        if not build_configs[entry].get("branch"):
            raise yaml.YAMLError(f"Branch not found for build config: {entry}'")
        if tree not in trees.keys():
            raise yaml.YAMLError(f"Tree {tree} not found in trees")


def validate_platforms(data):
    """
    Each entry in platforms must have arch, boot_method, mach
    If have compatible, it is must be a list
    """
    platforms = data.get("platforms")
    for entry in platforms:
        if entry == "docker" or entry == "kubernetes" or entry == "shell":
            continue
        if not platforms[entry].get("arch"):
            raise yaml.YAMLError(f"Arch not found for platform: {entry}'")
        if not platforms[entry].get("boot_method"):
            raise yaml.YAMLError(
                f"Boot method not found for platform: {entry}'"
            )
        if not platforms[entry].get("mach"):
            raise yaml.YAMLError(f"Mach not found for platform: {entry}'")
        if platforms[entry].get("compatible"):
            if not isinstance(platforms[entry].get("compatible"), list):
                raise yaml.YAMLError(
                    f"Compatible must be a list for platform: {entry}'"
                )


def validate_fragments(data):
    """
    Each entry in fragments must be a mapping with a 'path' key.
    Mirrors kernelci.config.build.Fragment.__init__ which requires
    'path' as a positional argument; a missing path there surfaces
    as a cryptic TypeError at pipeline startup.
    """
    fragments = data.get("fragments")
    if not fragments:
        return
    for name, entry in fragments.items():
        if not isinstance(entry, dict):
            raise yaml.YAMLError(
                f"Fragment '{name}' is not a mapping: {entry!r}"
            )
        if "path" not in entry:
            raise yaml.YAMLError(
                f"Fragment '{name}' is missing required 'path' key "
                f"(keys present: {list(entry.keys())})"
            )


def validate_rootfs_refs(data):
    """
    Validate rootfs references:
    1. Every rootfs_ref in job params resolves to an entry in rootfs section
    2. Rootfs entries are sorted alphabetically
    """
    rootfs_defs = data.get("rootfs", {})
    if not rootfs_defs:
        print("Warning: No rootfs definitions found")
        return

    # Check alphabetical sorting
    rootfs_names = list(rootfs_defs.keys())
    sorted_names = sorted(rootfs_names)
    if rootfs_names != sorted_names:
        raise yaml.YAMLError(
            f"Rootfs entries are not sorted alphabetically. "
            f"Expected order: {sorted_names}"
        )

    # Check all rootfs_ref values in jobs resolve
    jobs = data.get("jobs", {})
    for job_name, job_def in jobs.items():
        if not job_def or not isinstance(job_def, dict):
            continue
        params = job_def.get("params")
        if not params or not isinstance(params, dict):
            continue
        rootfs_ref = params.get("rootfs_ref")
        if rootfs_ref and rootfs_ref not in rootfs_defs:
            raise yaml.YAMLError(
                f"rootfs_ref '{rootfs_ref}' in job '{job_name}' "
                f"not found in rootfs definitions"
            )


def validate_unused_trees(data):
    """
    Check if all trees are used in build_configs
    """
    build_configs = data.get("build_configs")
    trees = data.get("trees")
    build_trees = [build_configs[entry].get("tree") for entry in build_configs]
    for tree in trees.keys():
        if tree not in build_trees:
            print(f"Warning: Tree {tree} is not used in build_configs")


def validate_duplicate_jobs(data, filename):
    """
    Check for duplicate job keys in a single YAML file
    This catches cases where the same job name appears multiple times
    """
    if "jobs" not in data:
        return

    # Parse the file as raw text to detect duplicate keys
    with open(filename, "r") as f:
        content = f.read()

    # Look for job definitions in the jobs section
    # Find the jobs section
    jobs_match = re.search(r"^jobs:\s*$", content, re.MULTILINE)
    if not jobs_match:
        return

    # Extract everything after the jobs: line until next top-level key or end of file
    jobs_start = jobs_match.end()
    jobs_section = content[jobs_start:]

    # Find the end of jobs section (next top-level key or end of file)
    next_section_match = re.search(r"^\S:", jobs_section, re.MULTILINE)
    if next_section_match:
        jobs_section = jobs_section[: next_section_match.start()]

    # Find all job names (lines that start with 2 spaces and contain a colon)
    job_pattern = r"^\s{2}([a-zA-Z0-9_\-]+):"
    job_matches = re.findall(job_pattern, jobs_section, re.MULTILINE)

    # Count occurrences of each job name
    job_counts = {}
    for job_name in job_matches:
        job_counts[job_name] = job_counts.get(job_name, 0) + 1

    # Report duplicates
    duplicates_found = False
    for job_name, count in job_counts.items():
        if count > 1:
            print(
                f"ERROR: Duplicate job '{job_name}' found {count} times in {filename}"
            )
            duplicates_found = True

    if duplicates_found:
        raise yaml.YAMLError(f"Duplicate job definitions found in {filename}")


def merge_files(dir="config"):
    """
    Merge all yaml files in the config/ directory
    """
    # Directories with their own validation (e.g. hardware_registry/)
    skip_dirs = {"hardware_registry"}
    merged_data = {}
    for file in glob.iglob(os.path.join(dir, "**", "*.yaml"), recursive=True):
        rel = os.path.relpath(file, dir)
        top_dir = rel.split(os.sep)[0]
        if top_dir in skip_dirs:
            continue
        print(f"Merging {file}")
        with open(file, "r") as stream:
            try:
                data = yaml.safe_load(stream)
                validate_duplicate_jobs(data, file)
                merged_data = recursive_merge(merged_data, data)
            except (yaml.YAMLError, ValueError) as exc:
                print(f"Error in {file}: {exc}")
                sys.exit(1)
    return merged_data


def validate_yaml(merged_data):
    """
    Validate all yaml files in the config/ directory
    """
    print("Validating jobs have a kcidb_test_suite mapping")
    validate_jobs(merged_data.get("jobs", {}))
    print("Validating scheduler entries to jobs")
    validate_scheduler_jobs(merged_data)
    validate_unused_jobs(merged_data)
    validate_build_configs(merged_data)
    validate_unused_trees(merged_data)
    validate_platforms(merged_data)
    print("Validating fragments")
    validate_fragments(merged_data)
    print("Validating rootfs references")
    validate_rootfs_refs(merged_data)
    print("All yaml files are valid")


def dumper(o_filename, merged_data):
    raw = yaml.dump(merged_data, Dumper=NoAliasDumper, indent=2)
    with open(o_filename, "w") as f:
        f.write(raw)
    print(f"Dumped merged data to {o_filename}")


def help():
    print("Usage: python validate_yaml.py -d <directory> -o <output_file>")
    print("Options:")
    print("-d, --dir       Directory to validate yaml files (default: config)")
    print(
        "-o, --output    Output file to dump merged yaml file without anchors"
    )
    print("-b, --base-ref  Git ref to compare scheduler coverage against")
    print("-h, --help      Show this help message and exit")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Validate and dump yaml files")
    parser.add_argument(
        "-d",
        "--dir",
        type=str,
        default="config",
        help="Directory to validate yaml files",
    )
    parser.add_argument(
        "-o", "--output", type=str, help="Output file to dump yaml files"
    )
    parser.add_argument(
        "-b",
        "--base-ref",
        type=str,
        help="Git ref to compare scheduler coverage against",
    )
    args = parser.parse_args()
    try:
        merged_data = merge_files(args.dir)
        if args.output:
            dumper(args.output, merged_data)

        validate_yaml(merged_data)
        if args.base_ref:
            report_dropped_schedules(args.base_ref, merged_data, args.dir)
    except (
        yaml.YAMLError,
        ValueError,
        OSError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"Validation failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
