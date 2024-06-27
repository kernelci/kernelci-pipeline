#!/usr/bin/env python3
'''
Validate all yaml files in the config/ directory
'''

import os
import yaml
import sys

def recursive_merge(d1, d2):
    '''
    Recursively merge two dictionaries, which might have lists
    Not sure if i done this right, but it works
    '''
    for k, v in d2.items():
        if k in d1:
            if isinstance(v, dict):
                d1[k] = recursive_merge(d1[k], v)
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
        if definition.get('kind') in ("test", "job"):
            if not definition.get('kcidb_test_suite'):
                raise yaml.YAMLError(
                    f"KCIDB test suite mapping not found for job: {name}'"
                )

def validate_scheduler_jobs(data):
    '''
    Each entry in scheduler have a job, that should be defined in jobs
    '''
    sch_entries = data.get('scheduler')
    jobs = data.get('jobs')
    for entry in sch_entries:
        if entry.get('job') not in jobs.keys():
            raise yaml.YAMLError(
                f"Job {entry.get('job')} not found in jobs"
            )

def validate_unused_jobs(data):
    '''
    Check if all jobs are used in scheduler
    '''
    sch_entries = data.get('scheduler')
    jobs = data.get('jobs')
    sch_jobs = [entry.get('job') for entry in sch_entries]
    for job in jobs.keys():
        if job not in sch_jobs:
            print(f"Warning: Job {job} is not used in scheduler")

def validate_yaml(dir='config/'):
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

if __name__ == '__main__':
    if len(sys.argv) > 1:
        validate_yaml(sys.argv[1])
    else:
        validate_yaml()
