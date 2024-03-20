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
# config/result-summary.yaml and as a template:
# config/result_summary_templates/result-preset.jinja2
#
# You can specify a date range for the searh using the --date-from
# (default: yesterday) and --date-to (default: now) options, formatted
# as YYYY-MM-DD or YYYY-MM-DDTHH:mm:SS (UTC)


# TODO:
# - HTML templates
# - Implement loop mode
# - Send email reports
# - Do we want to focus on regressions only or on any kind of result?
#   If we want test results as well:
#   - Provide logs for test leaf nodes
# - Tweak output and templates according to user needs
# - Parameterize/generalize templates to avoid duplication
# - Other suggested improvements

import sys
from datetime import datetime, timedelta, timezone
import logging

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


class ResultSummary(Service):
    def __init__(self, configs, args):
        super().__init__(configs, args, SERVICE_NAME)
        if args.verbose:
            self.log._logger.setLevel(logging.DEBUG)
        # self._config: the complete config file contents
        # self._preset_name: name of the selected preset to use
        # self._preset: loaded config for the selected preset
        with open(args.config, 'r') as config_file:
            self._config = yaml.safe_load(config_file)
        if args.preset:
            self._preset_name = args.preset
        else:
            self._preset_name = 'default'
        if self._preset_name not in self._config:
            self.log.error(f"No {self._preset_name} preset found in {args.config}")
            sys.exit(1)
        self._preset = self._config[self._preset_name]
        if args.from_date:
            self._from_date = args.from_date
        else:
            date = datetime.now(timezone.utc) - timedelta(days=1)
            self._from_date = date.strftime("%Y-%m-%dT%H:%M:%S")
        if args.to_date:
            self._to_date = args.to_date
        else:
            date = datetime.now(timezone.utc)
            self._to_date = date.strftime("%Y-%m-%dT%H:%M:%S")

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
        if self._from_date:
            base_params['created__gt'] = self._from_date
        if self._to_date:
            base_params['created__lt'] = self._to_date
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
                        new_repo[f'data.kernel_revision.{key}'] = value
                    repos.append(new_repo)
            for key, value in item.items():
                item_base_params[key] = value if value else 'null'
            if repos:
                for repo in repos:
                    query_params.append({**item_base_params, **repo})
            else:
                query_params.append(item_base_params)
        return query_params

    def _parse_config(self):
        """Processes and parses the selected preset configuration
        (self._preset) and returns the list of query parameters, where
        each list item is a complete set of query parameters.
        """
        params = []
        for block_name, body in self._preset.items():
            params.extend(self._parse_block_config(body, block_name, 'done'))
        return params

    def _check_log(self, url):
        """Sanity checks for a job output log, given its url. Returns
        True if the log exists and looks valid, False otherwise."""
        response = requests.get(url)
        if not len(response.content):
            return False
        return True

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
        template_env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(TEMPLATES_DIR)
        )
        template = template_env.get_template(f"{self._preset_name}.jinja2")
        params = self._parse_config()

        # Collect results
        for params_set in params:
            self.log.debug(f"Query: {params_set}")
            nodes = self._iterate_node_find(params_set)
            self.log.info(f"Total matches found: {len(nodes)}")

        # Filter log files
        # - remove empty files
        # - collect log files in a 'logs' field
        self.log.info(f"Checking logs ...")
        progress_total = len(nodes)
        progress = 0
        for node in nodes:
            logs = {}
            if node.get('artifacts'):
                all_logs = {item: url for item, url in node['artifacts'].items()
                            if item == 'log' or item.endswith('_log')}
                for log_name, url in all_logs.items():
                    if self._check_log(url):
                        logs[log_name] = url
            node['logs'] = logs
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
        # - from_date: start date of the results query
        # - to_date: end date of the results query
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
            'results_per_branch': results_per_branch,
            'from_date': self._from_date,
            'to_date': self._to_date,
        }
        output = template.render(template_params)
        self.log.info(output)
        return True


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
            'name': '--from-date',
            'help': ("Date from which to start collecting results "
                     "(YYYY-MM-DD). Default: one day before"),
        },
        {
            'name': '--to-date',
            'help': ("Collect results up to this date (YYYY-MM-DD). "
                     "Default: now"),
        },
        Args.verbose,
    ]

    def __call__(self, configs, args):
        return ResultSummary(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts(SERVICE_NAME, globals())
    yaml_configs = opts.get_yaml_configs() or 'config/pipeline.yaml'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
