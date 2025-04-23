#!/usr/bin/env python3
"""
Validate all yaml files in the config/ directory
"""

import os
import yaml
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
    for entry in schedules:
        if entry.get("job") not in jobs.keys():
            raise yaml.YAMLError(f"Job {entry.get('job')} not found in jobs")
        # if we have parameter: platforms, we need to make sure it exists in config
        if entry.get("platforms"):
            for platform in entry.get("platforms"):
                if platform not in data.get("platforms"):
                    raise yaml.YAMLError(f"Platform {platform} not found in platforms")


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


def merge_files(dir="config"):
    """
    Merge all yaml files in the config/ directory
    """
    merged_data = {}
    for file in os.listdir(dir):
        if file.endswith(".yaml"):
            print(f"Merging {file}")
            fpath = os.path.join(dir, file)
            with open(fpath, "r") as stream:
                try:
                    data = yaml.safe_load(stream)
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


if __name__ == "__main__":
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
