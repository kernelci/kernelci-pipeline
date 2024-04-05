# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo Navarro <ricardo.canuelo@collabora.com>
#
# Common utils for result-summary.

import gzip
import requests

import result_summary


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


def get_log(url, snippet_lines=0):
    """Fetches a text log given its url.

    Returns:
      If the log file couldn't be retrieved by any reason: None
      Otherwise:
        If snippet_lines == 0: the full log
        If snippet_lines > 0: the first snippet_lines log lines
        If snippet_lines < 0: the last snippet_lines log lines
    """
    response = requests.get(url)
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


def get_logs(node):
    """
    Retrieves and processes logs from a specified node.

    This method iterates over a node's 'artifacts', if present, to find log
    files. It identifies log files based on their item names, either being
    'log' or ending with '_log'. For each identified log file, it obtains
    the content by calling the `_get_log` method with the last 10 lines of
    the log. If the content is not empty, it then stores this log data in a
    dictionary, which includes both the URL of the log and its text content.
    Finally, it updates the 'logs' key of the input `node` with this
    dictionary of log data.

    Args:
        node (dict): A dictionary representing a node, which should contain
        an 'artifacts' key with log information.

    Modifies:
        The input `node` dictionary is modified in-place by adding a new key
        'logs', which contains a dictionary of processed log data. Each key
        in this 'logs' dictionary is a log name, and the corresponding value
        is another dictionary with keys 'url' (the URL of the log file) and
        'text' (the content of the log file).
    """
    logs = {}
    if node.get('artifacts'):
        all_logs = {item: url for item, url in node['artifacts'].items()
                    if item == 'log' or item.endswith('_log')}
        for log_name, url in all_logs.items():
            text = get_log(url, snippet_lines=-10)
            if text:
                logs[log_name] = {'url': url, 'text': text}
    node['logs'] = logs
