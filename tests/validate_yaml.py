#!/usr/bin/env python3
'''
Validate all yaml files in the config/ directory
'''

import os
import yaml
import sys

def recursive_merge(d1, d2, detect_same_keys=False):
    '''
    Recursively merge two dictionaries, which might have lists
    Not sure if i done this right, but it works
    '''
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
    '''
    Validate jobs, they must have a kcidb_test_suite mapping
    '''
    for name, definition in jobs.items():
        if not definition.get('kind'):
            raise yaml.YAMLError(
                f"Kind not found for job: {name}'"
            )
        if definition.get('kind') in ("test", "job"):
            if not definition.get('kcidb_test_suite'):
                raise yaml.YAMLError(
                    f"KCIDB test suite mapping not found for job: {name}'"
                )
        if definition.get('kind') == "job":
            if not definition.get('template'):
                raise yaml.YAMLError(
                    f"Template not found for job: {name}'"
                )

def validate_scheduler_jobs(data):
    '''
    Each entry in scheduler have a job, that should be defined in jobs
    '''
    schedules = data.get('scheduler')
    jobs = data.get('jobs')
    for entry in schedules:
        if entry.get('job') not in jobs.keys():
            raise yaml.YAMLError(
                f"Job {entry.get('job')} not found in jobs"
            )

def validate_unused_jobs(data):
    '''
    Check if all jobs are used in scheduler
    '''
    schedules = data.get('scheduler')
    jobs = data.get('jobs')
    sch_jobs = [entry.get('job') for entry in schedules]
    for job in jobs.keys():
        if job not in sch_jobs:
            print(f"Warning: Job {job} is not used in scheduler")

def validate_build_configs(data):
    '''
    Each entry in build_configs have a tree parameter
    This tree should exist in the trees: section
    '''
    build_configs = data.get('build_configs')
    trees = data.get('trees')
    for entry in build_configs:
        if build_configs[entry].get('tree') not in trees.keys():
            raise yaml.YAMLError(
                f"Tree {entry.get('tree')} not found in trees"
            )

def validate_unused_trees(data):
    '''
    Check if all trees are used in build_configs
    '''
    build_configs = data.get('build_configs')
    trees = data.get('trees')
    build_trees = [build_configs[entry].get('tree') for entry in build_configs]
    for tree in trees.keys():
        if tree not in build_trees:
            print(f"Warning: Tree {tree} is not used in build_configs")

def validate_yaml(dir='config'):
    '''
    Validate all yaml files in the config/ directory
    '''
    merged_data = {}
    for file in os.listdir(dir):
        if file.endswith('.yaml'):
            print(f"Validating {file}")
            fpath = os.path.join(dir, file)
            with open(fpath, 'r') as stream:
                try:
                    data = yaml.safe_load(stream)
                    merged_data = recursive_merge(merged_data, data)
                    jobs = data.get('jobs')
                    if jobs:
                        validate_jobs(jobs)
                except yaml.YAMLError as exc:
                    print(f'Error in {file}: {exc}')
                    sys.exit(1)
    print("Validating scheduler entries to jobs")
    validate_scheduler_jobs(merged_data)
    validate_unused_jobs(merged_data)
    validate_build_configs(merged_data)
    validate_unused_trees(merged_data)

if __name__ == '__main__':
    if len(sys.argv) > 1:
        validate_yaml(sys.argv[1])
    else:
        validate_yaml()
