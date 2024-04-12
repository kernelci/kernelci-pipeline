# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo Navarro <ricardo.canuelo@collabora.com>
#
# summary-mode-specifc code for result-summary.

import concurrent.futures
from datetime import datetime, timedelta, timezone
import os

import result_summary
import result_summary.utils as utils

_date_params = {
    'created_from': 'created__gt',
    'created_to': 'created__lt',
    'last_updated_from': 'updated__gt',
    'last_updated_to': 'updated__lt'
}


def setup(service, args, context):
    # Additional date parameters
    date_params = {}
    if args.created_from:
        date_params[_date_params['created_from']] = args.created_from
    if args.created_to:
        date_params[_date_params['created_to']] = args.created_to
    if args.last_updated_from:
        date_params[_date_params['last_updated_from']] = args.last_updated_from
    if args.last_updated_to:
        date_params[_date_params['last_updated_to']] = args.last_updated_to
    # Default if no dates are specified: created since yesterday
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    if not any([args.created_from,
                args.created_to,
                args.last_updated_from,
                args.last_updated_to]):
        date_params[_date_params['created_from']] = yesterday.strftime("%Y-%m-%dT%H:%M:%S")
    if not args.created_to and not args.last_updated_to:
        if args.last_updated_from:
            date_params[_date_params['last_updated_to']] = now_str
        else:
            date_params[_date_params['created_to']] = now_str
    return {'date_params': date_params}


def stop(service, context):
    pass


def run(service, context):
    # Run queries and collect results
    nodes = []
    context['metadata']['queries'] = []
    for params_set in context['preset_params']:
        # Apply date range parameters, if defined
        params_set.update(context['date_params'])
        # Apply extra query parameters from command line, if any
        params_set.update(context['extra_query_params'])
        result_summary.logger.debug(f"Query: {params_set}")
        context['metadata']['queries'].append(params_set)
        query_results = utils.iterate_node_find(service, params_set)
        result_summary.logger.debug(f"Query matches found: {len(query_results)}")
        nodes.extend(query_results)
    result_summary.logger.info(f"Total nodes found: {len(nodes)}")

    # Post-process nodes
    # Filter log files
    # - remove empty files
    # - collect log files in a 'logs' field
    result_summary.logger.info(f"Post-processing nodes ...")
    progress_total = len(nodes)
    progress = 0
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = {executor.submit(utils.post_process_node, node, service._api) for node in nodes}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            progress += 1
            if progress >= progress_total / 10:
                print('.', end='', flush=True)
                progress = 0
    print('', flush=True)

    # Group results by tree/branch
    results_per_branch = {}
    for node in nodes:
        if node['data'].get('failed_kernel_version'):
            tree = node['data']['failed_kernel_version']['tree']
            branch = node['data']['failed_kernel_version']['branch']
        else:
            tree = node['data']['kernel_revision']['tree']
            branch = node['data']['kernel_revision']['branch']
        if tree not in results_per_branch:
            results_per_branch[tree] = {branch: [node]}
        else:
            if branch not in results_per_branch[tree]:
                results_per_branch[tree][branch] = [node]
            else:
                results_per_branch[tree][branch].append(node)

    # Data provided to the templates:
    # - metadata: preset-specific metadata
    # - query date specifications and ranges:
    #     created_to, created_from, last_updated_to, last_updated_from
    # - results_per_branch: a dict containing the result nodes
    #   grouped by tree and branch like this:
    #
    #   results_per_branch = {
    #       <tree_1>: {
    #           <branch_1>: [
    #               node_1,
    #               ...
    #               node_n
    #           ],
    #           ...,
    #           <branch_n>: ...
    #       },
    #       ...,
    #       <tree_n>: ...
    #   }
    template_params = {
        'metadata': context['metadata'],
        'results_per_branch': results_per_branch,
        # Optional parameters
        'created_from': context['date_params'].get(_date_params['created_from']),
        'created_to': context['date_params'].get(_date_params['created_to']),
        'last_updated_from': context['date_params'].get(_date_params['last_updated_from']),
        'last_updated_to': context['date_params'].get(_date_params['last_updated_to']),
    }
    output_text = context['template'].render(template_params)
    # Setup output dir from base path and user-specified
    # parameter (in preset metadata or cmdline)
    output_dir = result_summary.BASE_OUTPUT_DIR
    if context.get('output_dir'):
        output_dir = os.path.join(output_dir, context['output_dir'])
    elif 'output_dir' in context['metadata']:
        output_dir = os.path.join(output_dir, context['metadata']['output_dir'])
    os.makedirs(output_dir, exist_ok=True)
    # Generate and dump output
    # output_file specified in cmdline:
    output_file = context['output_file']
    if not output_file:
        # Check if output_file is specified as a preset parameter
        if 'output_file' in context['metadata']:
            output_file = context['metadata']['output_file']
    if output_file:
        output_file = os.path.join(output_dir, output_file)
        with open((output_file), 'w') as outfile:
            outfile.write(output_text)
    else:
        result_summary.logger.info(output_text)
    return True
