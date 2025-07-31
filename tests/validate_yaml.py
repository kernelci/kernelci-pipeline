#!/usr/bin/env python3
"""
Validate all yaml files in the config/ directory
"""

import glob
import os
import yaml
import re
import sys
import argparse


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
                raise ValueError(f"Key {k} has different values in both dictionaries")
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
                    raise yaml.YAMLError(f"Test method not found for job: {name}'")
                if params.get("fragments"):
                    raise yaml.YAMLError(f"Fragments not allowed in jobs: {name}'")


def validate_scheduler_jobs(data):
    """
    Each entry in scheduler have a job, that should be defined in jobs
    """
    schedules = data.get("scheduler")
    jobs = data.get("jobs")
    runtimes = data.get("runtimes")
    for entry in schedules:
        jobname = entry.get("job")
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
            raise yaml.YAMLError(f"Channel not found for event: {jobname}: {entry}")
        if not event.get("state"):
            raise yaml.YAMLError(f"State not found for event: {jobname}: {entry}")
        if not event.get("kind"):
            raise yaml.YAMLError(f"Kind not found for event: {jobname}: {entry}")
        # if we have parameter: platforms, we need to make sure it exists in config
        if entry.get("platforms"):
            for platform in entry.get("platforms"):
                if platform not in data.get("platforms"):
                    raise yaml.YAMLError(f"Platform {platform} not found in platforms")
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
        if not tree in trees.keys():
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
            raise yaml.YAMLError(f"Boot method not found for platform: {entry}'")
        if not platforms[entry].get("mach"):
            raise yaml.YAMLError(f"Mach not found for platform: {entry}'")
        if platforms[entry].get("compatible"):
            if not isinstance(platforms[entry].get("compatible"), list):
                raise yaml.YAMLError(
                    f"Compatible must be a list for platform: {entry}'"
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
    merged_data = {}
    for file in glob.iglob(os.path.join(dir, "**", "*.yaml"), recursive=True):
        print(f"Merging {file}")
        with open(file, "r") as stream:
            try:
                data = yaml.safe_load(stream)
                validate_duplicate_jobs(data, file)
                merged_data = recursive_merge(merged_data, data)
            except yaml.YAMLError as exc:
                print(f"Error in {file}: {exc}")
                sys.exit(1)
    return merged_data


def validate_yaml(merged_data):
    """
    Validate all yaml files in the config/ directory
    """
    print("Validating scheduler entries to jobs")
    validate_scheduler_jobs(merged_data)
    validate_unused_jobs(merged_data)
    validate_build_configs(merged_data)
    validate_unused_trees(merged_data)
    validate_platforms(merged_data)
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
    print("-o, --output    Output file to dump merged yaml file without anchors")
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
    args = parser.parse_args()
    merged_data = merge_files(args.dir)
    if args.output:
        dumper(args.output, merged_data)

    validate_yaml(merged_data)


if __name__ == "__main__":
    main()
