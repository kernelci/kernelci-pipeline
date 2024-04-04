#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo Navarro <ricardo.canuelo@collabora.com>

# KernelCI client code to retrieve and summarize job (test, regressions)
# results
#
# How to use this (for now):
#
#        docker-compose run result_summary --preset=<result-preset>
#
# where <result-preset> is defined as a query preset definition in
# config/result-summary.yaml.

# You can specify a date range for the searh using the --date-from
# (default: yesterday) and --date-to (default: now) options, formatted
# as YYYY-MM-DD or YYYY-MM-DDTHH:mm:SS (UTC)
#
# Each preset may define the name of the output file generated (in
# data/output). This can be overriden with the --output option. If no
# output file is defined, the output will be printed to stdout.
#
# For current status info, see the development changelog in
# config/result_summary_templates/CHANGELOG

# TODO:
# - Refactor liberally
# - Implement loop mode
# - Send email reports
# - Do we want to focus on regressions only or on any kind of result?
#   If we want test results as well:
#   - Provide logs for test leaf nodes
# - Tweak output and templates according to user needs
# - Parameterize/generalize templates to avoid duplication
# - Other suggested improvements

import sys
import concurrent.futures
from datetime import datetime, timedelta, timezone
import gzip
import logging
import os
import re
import shutil

import jinja2
import json
import requests
import yaml

import kernelci
import kernelci.api.models as models
from kernelci.legacy.cli import Args, Command, parse_opts
from base import Service

SERVICE_NAME = 'result_summary'
TEMPLATES_DIR = './config/result_summary_templates/'
OUTPUT_DIR = '/home/kernelci/data/output/'


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


class ResultSummary(Service):
    def __init__(self, configs, args):
        super().__init__(configs, args, SERVICE_NAME)
        if args.verbose:
            self.log._logger.setLevel(logging.DEBUG)
        self._template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES_DIR)
        )

    def _parse_block_config(self, block, kind, state):
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

    def _get_log(self, url, snippet_lines=0):
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
            # self.log.info(f"Lines: {lines}")
            # self.log.info(f"Number of lines: {len(lines)}")
            return '\n'.join(lines[snippet_lines:])
        return text

    def _get_logs(self, node):
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
                text = self._get_log(url, snippet_lines=-10)
                if text:
                    logs[log_name] = {'url': url, 'text': text}
        node['logs'] = logs

    def _setup(self, args):
        # Load and sanity check command line parameters
        # config: the complete config file contents
        # preset_name: name of the selected preset to use
        # preset: loaded config for the selected preset
        with open(args.config, 'r') as config_file:
            config = yaml.safe_load(config_file)
        if args.preset:
            preset_name = args.preset
        else:
            preset_name = 'default'
        if preset_name not in config:
            self.log.error(f"No {preset_name} preset found in {args.config}")
            sys.exit(1)
        preset = config[preset_name]
        # Additional query parameters
        extra_query_params = {}
        if args.query_params:
            extra_query_params = split_query_params(args.query_params)
        output = None
        if args.output:
            output = args.output
        # End of command line argument loading and sanity checks

        # Load presets
        metadata = {}
        preset_params = []
        if 'metadata' in preset:
            metadata = preset['metadata']
        for block_name, body in preset['preset'].items():
            preset_params.extend(self._parse_block_config(body, block_name, 'done'))
        if 'template' not in metadata:
            self.log.error(f"No template defined for preset {preset_name}")
            sys.exit(1)
        template = self._template_env.get_template(metadata['template'])
        return {'metadata': metadata,
                'preset_params': preset_params,
                'extra_query_params': extra_query_params,
                'template': template,
                'output': output,
                }


class ResultSummarySingle(ResultSummary):
    _date_params = {
        'created_from': 'created__gt',
        'created_to': 'created__lt',
        'last_updated_from': 'updated__gt',
        'last_updated_to': 'updated__lt'
    }

    def _setup(self, args):
        ctx = super()._setup(args)
        # Additional date parameters
        date_params = {}
        if args.created_from:
            date_params[self._date_params['created_from']] = args.created_from
        if args.created_to:
            date_params[self._date_params['created_to']] = args.created_to
        if args.last_updated_from:
            date_params[self._date_params['last_updated_to']] = args.last_updated_from
        if args.last_updated_to:
            date_params[self._date_params['last_updated_from']] = args.last_updated_to
        # Default if no dates are specified: created since yesterday
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1))
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        if not any([args.created_from,
                    args.created_to,
                    args.last_updated_from,
                    args.last_updated_to]):
            date_params[self._date_params['created_from']] = yesterday.strftime("%Y-%m-%dT%H:%M:%S")
        if not args.created_to and not args.last_updated_to:
            if args.last_updated_from:
                date_params[self._date_params['last_updated_to']] = now_str
            else:
                date_params[self._date_params['created_to']] = now_str
        ctx['date_params'] = date_params
        return ctx

    def _iterate_node_find(self, params):
        """Request a node search to the KernelCI API based on a set of
        search parameters (a dict). The search is split into iterative
        limited searches.

        Returns the list of nodes found, or an empty list if the search
        didn't find any.
        """
        nodes = []
        limit = 100
        offset = 0
        self.log.info("Searching")
        while True:
            search = self._api.node.find(params, limit=limit, offset=offset)
            print(".", end='', flush=True)
            if not search:
                break
            nodes.extend(search)
            offset += limit
        print("", flush=True)
        return nodes

    def _run(self, ctx):
        # Run queries and collect results
        nodes = []
        ctx['metadata']['queries'] = []
        for params_set in ctx['preset_params']:
            # Apply date range parameters, if defined
            params_set.update(ctx['date_params'])
            # Apply extra query parameters from command line, if any
            params_set.update(ctx['extra_query_params'])
            self.log.debug(f"Query: {params_set}")
            ctx['metadata']['queries'].append(params_set)
            query_results = self._iterate_node_find(params_set)
            self.log.debug(f"Query matches found: {len(query_results)}")
            nodes.extend(query_results)
        self.log.info(f"Total nodes found: {len(nodes)}")

        # Filter log files
        # - remove empty files
        # - collect log files in a 'logs' field
        self.log.info(f"Checking logs ...")
        progress_total = len(nodes)
        progress = 0
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = {executor.submit(self._get_logs, node) for node in nodes}
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
            'metadata': ctx['metadata'],
            'results_per_branch': results_per_branch,
            # Optional parameters
            'created_from': ctx['date_params'].get(self._date_params['created_from']),
            'created_to': ctx['date_params'].get(self._date_params['created_to']),
            'last_updated_from': ctx['date_params'].get(self._date_params['last_updated_from']),
            'last_updated_to': ctx['date_params'].get(self._date_params['last_updated_to']),
        }
        output_text = ctx['template'].render(template_params)
        output = ctx['output']
        if not output:
            if 'output' in ctx['metadata']:
                output = ctx['metadata']['output']
        if output:
            with open(os.path.join(OUTPUT_DIR, output), 'w') as output_file:
                output_file.write(output_text)
            shutil.copy(os.path.join(TEMPLATES_DIR, 'main.css'), OUTPUT_DIR)
        else:
            self.log.info(output_text)
        return True


class ResultSummaryLoop(ResultSummary):
    def _run(self, ctx):
        pass


class cmd_run(Command):
    help = ("Checks for test results in a specific date range "
            "and generates summary reports (single shot)")
    args = [
        {
            'name': '--config',
            'help': "Path to service-specific config yaml file",
        },
    ]
    opt_args = [
        {
            'name': '--preset',
            'help': "Configuration preset to load ('default' if none)",
        },
        {
            'name': '--created-from',
            'help': ("Collect results created since this date and time"
                     "(YYYY-mm-DDTHH:MM:SS). Default: since last 24 hours"),
        },
        {
            'name': '--created-to',
            'help': ("Collect results created up to this date and time "
                     "(YYYY-mm-DDTHH:MM:SS). Default: until now"),
        },
        {
            'name': '--last-updated-from',
            'help': ("Collect results that were last updated since this date and time"
                     "(YYYY-mm-DDTHH:MM:SS). Default: since last 24 hours"),
        },
        {
            'name': '--last-updated-to',
            'help': ("Collect results that were last updated up to this date and time "
                     "(YYYY-mm-DDTHH:MM:SS). Default: until now"),
        },
        {
            'name': '--output',
            'help': "Override the 'output' preset parameter"
        },
        {
            'name': '--query-params',
            'help': ("Additional query parameters: "
                     "'<paramX>=<valueX>,<paramY>=<valueY>'")
        },
        Args.verbose,
    ]

    def __call__(self, configs, args):
        return ResultSummarySingle(configs, args).run(args)


class cmd_loop(Command):
    help = ("Checks for test results in a specific date range "
            "and generates summary reports (single shot)")
    args = [
        {
            'name': '--config',
            'help': "Path to service-specific config yaml file",
        },
    ]
    opt_args = [
        {
            'name': '--preset',
            'help': "Configuration preset to load ('default' if none)",
        },
        {
            'name': '--output',
            'help': "Override the 'output' preset parameter"
        },
        {
            'name': '--query-params',
            'help': ("Additional query parameters: "
                     "'<paramX>=<valueX>,<paramY>=<valueY>'")
        },
        Args.verbose,
    ]

    def __call__(self, configs, args):
        args.cmd = 'loop'
        return ResultSummary(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts(SERVICE_NAME, globals())
    yaml_configs = opts.get_yaml_configs() or 'config/pipeline.yaml'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
