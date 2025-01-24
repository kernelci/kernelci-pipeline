#!/usr/bin/env python3
"""
KernelCI API example
3rd party lab submission, when lab is not integrated with KernelCI pipeline

"""

import requests
import json
import sys

# KernelCI API URL
api_url = "https://staging.kernelci.org:9000/latest"


def add_authorization_header(headers):
    # read api token from .api_token file
    try:
        with open(".api_token", "r") as f:
            api_token = f.read().strip()
    except FileNotFoundError:
        print("Error: API token file not found")
        sys.exit(1)
    # Add the authorization header to the request
    headers["Authorization"] = f"Bearer {api_token}"
    return headers


def retrieve_node(node_id):
    url = api_url + "/node/" + node_id
    headers = {"Content-Type": "application/json"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print("Failed to retrieve node:", response.text)
        return None
    return response.json()


def create_node(node):
    # Create a new node in the database
    url = api_url + "/node"
    headers = {"Content-Type": "application/json"}
    headers = add_authorization_header(headers)
    response = requests.post(url, headers=headers, data=json.dumps(node))
    if response.status_code != 200:
        print("Failed to create node:", response.text)
        return None
    try:
        return response.json()
    except json.JSONDecodeError:
        print("Failed to create node:", response.text)
        return None


def update_node_tree(node_id, treedata):
    # Update the node with a new tree
    url = api_url + "/nodes/" + node_id
    headers = {"Content-Type": "application/json"}
    headers = add_authorization_header(headers)
    print(treedata)
    response = requests.put(url, headers=headers, data=json.dumps(treedata))
    if response.status_code != 200:
        print("Failed to update node tree:", response.text)
        return None
    return response


def create_job(jobname, parent_node):
    job_node = {
        "kind": "job",
        "name": jobname,
        "path": ["checkout", jobname],
        "group": jobname,
        "parent": parent_node["id"],
        "state": "running",
        "result": None,
        "artifacts": {},
        "data": {},
        "submitter": parent_node["submitter"],
        "treeid": parent_node["treeid"],
    }
    job_node["data"]["kernel_revision"] = parent_node["data"]["kernel_revision"]
    return create_node(job_node)


def submit_job_results(job_node_id, tests, artifacts):
    """
    tests supplied as a list of
    {
        "name": "testname",
        "result": "pass" or "fail" or "skip",
        "platform": "platformname",
        "runtime": "lab-abcde,

    }
    WARNING: platform must be defined in kernelci-pipeline instance
    It is also preferable to use fixed name for your lab, set in runtime field.
    """
    job_node = retrieve_node(job_node_id)
    child_nodes = []
    # iterate over tests and create child_nodes
    for test in tests:
        nodepath = job_node["path"]
        nodepath.append(test["name"])
        test_node = {
            "kind": "test",
            "name": test["name"],
            "path": nodepath,
            "group": test["name"],
            "parent": job_node["id"],
            "state": "done",
            "result": test["result"],
            "artifacts": {},
            "data": {},
            "submitter": job_node["submitter"],
            "treeid": job_node["treeid"],
        }
        test_node["data"]["kernel_revision"] = job_node["data"]["kernel_revision"]
        test_node["data"]["platform"] = test["platform"]
        test_node["data"]["runtime"] = test["runtime"]
        child_node = {"node": test_node, "child_nodes": []}
        child_nodes.append(child_node)

    # update job_node state to done
    job_node["state"] = "done"
    # update also result (it might be dependent on test results)
    job_node["result"] = (
        "fail" if any(test["result"] == "fail" for test in tests) else "pass"
    )

    treedata = {"node": job_node, "child_nodes": child_nodes}

    return update_node_tree(job_node["id"], treedata)


def main():
    # create sample job node and then update with sample test results
    job_name = "job-12346"
    tests = [
        {
            "name": "test1",
            "result": "pass",
            "platform": "qemu",
            "runtime": "lab-abcde",
        },
        {
            "name": "test2",
            "result": "fail",
            "platform": "qemu-x86",
            "runtime": "lab-abcde",
        },
    ]
    # you need to place here log files, etc.
    # ex:
    # "artifacts": {
    # "lava_log": "http://mon.kernelci.org:3000/baseline-x86-679351bb4c0614bf480e125d/log.txt.gz",
    # "callback_data": "http://mon.kernelci.org:3000/baseline-x86-679351bb4c0614bf480e125d/lava_callback.json.gz"
    # },
    # kcidb bridge will search for lava_log or test_log
    artifacts = {}
    # build node id, kernel which we used for testing
    parent_node_id = "67934c4b4c0614bf480df84f"

    # create job node that defines the lab job
    job_response = create_job(job_name, retrieve_node(parent_node_id))
    if job_response is None:
        print("Failed to create job node")
        return

    job_node_id = job_response["id"]
    print("Created job node:", job_node_id)

    # Here you retrieve results from your lab and fill tests

    # update job node with test results and set it to completed
    submit_job_results(job_node_id, tests, artifacts)


if __name__ == "__main__":
    main()
