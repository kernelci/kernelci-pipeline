#!/usr/bin/env python3
"""
Validate all yaml files in the config/ directory
"""

import glob
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
        jobname = entry.get("job")
        if jobname not in jobs.keys():
            raise yaml.YAMLError(f"Job {jobname} not found in jobs")
        # scheduler entry must have defined in event: channel, (state or result), kind
        event = entry.get("event")
        if not event:
            raise yaml.YAMLError(f"Event not found for scheduler entry: {jobname}: {entry}")
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
    for file in glob.iglob(os.path.join(dir, "**", "*.yaml"), recursive=True):
        print(f"Merging {file}")
        with open(file, "r") as stream:
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

def validate_rules(node, rules):
    """
    Validate rules for a given node
    """
    import kernelci.api.helper
    helper = kernelci.api.helper.APIHelper(None)
    if helper.should_create_node(rules, node):
        #print(f"Node {node} matches rules: {rules}")
        return True
    else:
        #print(f"Node {node} does not match rules: {rules}")
        return False

def compare_builds(merged_data):
    """
    Compare kbuilds and print builds with identical params
    """
    r = ""
    jobs = merged_data.get("jobs")
    kbuilds_list = []
    for job in jobs:
        if jobs[job].get("kind") == "kbuild":
            kbuilds_list.append(job)

    kbuilds_dict = {}
    import json
    for kbuild in kbuilds_list:
        params = jobs[kbuild].get("params", {})
        # Convert params to a hashable type by serializing to JSON
        key = json.dumps(params, sort_keys=True)
        if key not in kbuilds_dict:
            kbuilds_dict[key] = []
        kbuilds_dict[key].append(kbuild)

    # print builds with identical params
    for params, kbuild_list in kbuilds_dict.items():
        if len(kbuild_list) > 1:
            r += f"Params {params}: {kbuild_list},"

    return r


def walker(merged_data):
    """
    We will simulate checkout event on each tree/branch
    and try to build list of builds/tests it will run
    """
    checkouts = []
    build_configs = merged_data.get("build_configs", {})
    for bcfg in build_configs:
        data = build_configs[bcfg]
        if not data.get("architectures"):
            data["architectures"] = None
        checkouts.append(data)

    # sort checkouts by tree and branch
    checkouts.sort(key=lambda x: (x.get("tree", ""), x.get("branch", "")))

    # iterate over checkouts
    for checkout in checkouts:
        checkout["kbuilds"] = []
        # iterate over events (jobs)
        jobs = merged_data.get("scheduler", [])
        for job in jobs:
            kind = job.get("event", {}).get("kind")
            if kind != "checkout":
                continue
            job_name = job.get("job")
            job_kind = merged_data.get("jobs", {}).get(job_name, {}).get("kind")
            if job_kind == "kbuild":
                # check "params" "arch"
                job_params = merged_data.get("jobs", {}).get(job_name, {}).get("params", {})
                arch = job_params.get("arch")
                if checkout.get("architectures") and arch not in checkout.get("architectures"):
                    continue
            scheduler_rules = job.get("rules", [])
            job = merged_data.get("jobs", {}).get(job_name, {})
            job_rules = job.get("rules", [])
            node = {
                "kind": "checkout",
                "data": {
                    "kernel_revision": {
                        "tree": checkout.get("tree"),
                        "branch": checkout.get("branch"),
                        "version": {
                            "version": 6,
                            "patchlevel": 16,
                            "extra": "-rc3-973-gb7d1bbd97f77"
                        },
                    }
                },
            }
            if not validate_rules(node, job_rules):
                continue
            if not validate_rules(node, scheduler_rules):
                continue
            checkout["kbuilds"].append(job_name)
        checkout["kbuilds_identical"] = compare_builds(merged_data)

    # print the results
    for checkout in checkouts:
        print(f"Checkout: {checkout.get('tree')}:{checkout.get('branch')}")
        if checkout.get("kbuilds_identical"):
            print(f"  Identical builds: {checkout['kbuilds_identical']}")
        if checkout.get("kbuilds"):
            num_builds = len(checkout["kbuilds"])
            print(f"  Number of builds: {num_builds}")
            print("  Builds:")
            for build in checkout["kbuilds"]:
                print(f"    - {build}")
        else:
            print("  No builds found for this checkout")
    return


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
    parser.add_argument(
        "-w",
        "--walker",
        action="store_true",
        help="Simulate checkout event on each tree/branch",
    )
    args = parser.parse_args()
    merged_data = merge_files(args.dir)
    if args.output:
        dumper(args.output, merged_data)

    if args.walker:
        walker(merged_data)
        sys.exit(0)

    validate_yaml(merged_data)
