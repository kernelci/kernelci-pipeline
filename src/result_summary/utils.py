# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo Navarro <ricardo.canuelo@collabora.com>
#
# Common utils for result-summary.

import gzip
import re
import requests
import yaml
from typing import Any, Dict

import result_summary


CONFIG_TRACES_FILE_PATH = './config/traces_config.yaml'


def split_query_params(query_string):
    """Given a string input formatted like this:
           parameter1=value1,parameter2=value2,...,parameterN=valueN
       return a dict containing the string information where the
       parameters are the dict keys:
           {'parameter1': 'value1',
            'parameter2': 'value2',
            ...,
            'parameterN': 'valueN'
           }
    """
    query_dict = {}
    matches = re.findall('([^ =,]+)\s*=\s*([^ =,]+)', query_string)  # noqa: W605
    for operator, value in matches:
        query_dict[operator] = value
    return query_dict


def parse_block_config(block, kind, state):
    """Parse a config block. Every block may define a set of
    parameters, including a list of 'repos' (trees/branches). For
    every 'repos' item, this method will generate a query parameter
    set. All the query parameter sets will be based on the same base
    params.

    If the block doesn't define any repos, there'll be only one
    query parameter set created.

    If the block definition is empty, that is, if there aren't any
    specific query parameters, just return the base query parameter
    list.

    Returns a list of query parameter sets.

    """
    # Base query parameters include the node kind and state and the
    # date ranges if defined
    base_params = {
        'kind': kind,
        'state': state,
    }
    kernel_revision_field = 'data.kernel_revision'
    if kind == 'regression':
        kernel_revision_field = 'data.failed_kernel_version'
    if not block:
        return [{**base_params}]
    query_params = []
    for item in block:
        item_base_params = base_params.copy()
        repos = []
        if 'repos' in item:
            for repo in item.pop('repos'):
                new_repo = {}
                for key, value in repo.items():
                    new_repo[f'{kernel_revision_field}.{key}'] = value
                repos.append(new_repo)
        for key, value in item.items():
            item_base_params[key] = value if value else 'null'
        if repos:
            for repo in repos:
                query_params.append({**item_base_params, **repo})
        else:
            query_params.append(item_base_params)
    return query_params


def iterate_node_find(service, params):
    """Request a node search to the KernelCI API based on a set of
    search parameters (a dict). The search is split into iterative
    limited searches.

    Returns the list of nodes found, or an empty list if the search
    didn't find any.
    """
    nodes = []
    limit = 100
    offset = 0
    result_summary.logger.info("Searching")
    while True:
        search = service._api.node.find(params, limit=limit, offset=offset)
        print(".", end='', flush=True)
        if not search:
            break
        nodes.extend(search)
        offset += limit
    print("", flush=True)
    return nodes


def get_err_category(trace: str, traces_config: Dict) -> Dict[str, Any]:
    """Given a trace and a traces config, return its category"""
    # sourcery skip: raise-specific-error
    for category in traces_config["categories"]:
        p = "|".join(category["patterns"])
        if re.findall(p, trace or ""):
            return category
    raise Exception(f"No category found")


def get_log(url, snippet_lines=0):
    """Fetches a text log given its url.

    Returns:
      If the log file couldn't be retrieved by any reason: None
      Otherwise:
        If snippet_lines == 0: the full log
        If snippet_lines > 0: the first snippet_lines log lines
        If snippet_lines < 0: the last snippet_lines log lines
    """
    try:
        response = requests.get(url)
    except:
        # Bail out if there was any error fetching the log
        return None
    if not len(response.content):
        return None
    try:
        raw_bytes = gzip.decompress(response.content)
        text = raw_bytes.decode('utf-8')
    except gzip.BadGzipFile:
        text = response.text
    if snippet_lines > 0:
        lines = text.splitlines()
        return '\n'.join(lines[:snippet_lines])
    elif snippet_lines < 0:
        lines = text.splitlines()
        return '\n'.join(lines[snippet_lines:])
    return text


def artifact_is_log(artifact_name):
    """Returns True if artifact_name looks like a log artifact, False
    otherwise"""
    possible_log_names = [
        'job_txt',
    ]
    if (artifact_name == 'log' or
        artifact_name.endswith('_log') or
        artifact_name in possible_log_names):
        return True
    return False


def get_logs(node):
    """Retrieves and processes logs from a specified node.

    This method iterates over a node's 'artifacts', if present, to find
    log files. For each identified log file, it obtains the content by
    calling the `_get_log` method.
    If the content is not empty, it then stores this log data in a
    dictionary, which includes both the URL of the log and its text
    content.

    If the node log points to an empty file, the dict will contain an
    entry for the log with an empty value.

    Args:
        node (dict): A dictionary representing a node, which should contain
        an 'artifacts' key with log information.

    Returns:
        A dict with an entry per node log. If the node log points to an
        empty file, the entry will have an emtpy value. Otherwise, the
        value will be a dict containing the 'url' and collected 'text'
        of the log (may be an excerpt)

        None if no logs were found.
    """
    if node.get('artifacts'):
        logs = {}
        log_fields = {}
        for artifact, value in node['artifacts'].items():
            if artifact_is_log(artifact):
                log_fields[artifact] = value
        for log_name, url in log_fields.items():
            text = get_log(url)
            if text:
                logs[log_name] = {'url': url, 'text': text}
            else:
                logs[log_name] = None
        return logs
    return None


def post_process_node(node, api):
    """Runs a set of operations to post-proces and extract additional
    information for a node:

    - Find/complete/process node logs

    Modifies:
        The input `node` dictionary is modified in-place by adding a new
        key 'logs', which contains a dictionary of processed log
        data (see get_logs()).
    """

    def find_node_logs(node, api):
        """For an input node, use get_logs() to retrieve its log
        artifacts. If no log artifacts were found in the node, search
        upwards through parent links until finding one node in the chain
        that contains logs.

        Returns:
            A dict as returned by get_logs, but without empty log entries.
        """
        logs = get_logs(node)
        if not logs:
            if node.get('parent'):
                parent = api.node.get(node['parent'])
                if parent:
                    logs = find_node_logs(parent, api)
        if not logs:
            return {}
        # Remove empty logs
        return {k: v for k, v in logs.items() if v}

    def log_snippets_only(logs, snippet_lines):
        for log in logs:
            lines = logs[log]['text'].splitlines()
            logs[log]['text'] = '\n'.join(lines[-snippet_lines:])
        return logs

    def concatenate_logs(logs):
        concatenated_logs = ''
        for log in logs:
            concatenated_logs += logs[log]['text']
        return concatenated_logs

    node['logs'] = find_node_logs(node, api)

    if node['result'] != 'pass':
        concatenated_logs = concatenate_logs(node['logs'])

        with open(CONFIG_TRACES_FILE_PATH) as f:
            traces_config: Dict[str, Any] = yaml.load(f, Loader=yaml.FullLoader)
        node['category'] = get_err_category(concatenated_logs, traces_config)

    # Only get the last 10 lines of the log
    snippet_lines = 10
    node['logs'] = log_snippets_only(node['logs'], snippet_lines)
