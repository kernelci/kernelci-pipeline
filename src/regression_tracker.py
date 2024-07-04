#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import sys

import json

import kernelci
import kernelci.config
import kernelci.db
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service


class RegressionTracker(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'regression_tracker')

    def _setup(self, args):
        return self._api_helper.subscribe_filters({
            'state': 'done',
        })

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)

    def _collect_logs(self, node):
        """Returns a dict containing the log artifacts of <node>. Log
        artifacts are those named 'log' or whose name contains the
        '_log' suffix. If <node> doesn't have any artifacts, the
        search will continue upwards through parent nodes until reaching
        a node that has them.
        """
        logs = {}
        if node.get('artifacts'):
            for artifact, value in node['artifacts'].items():
                if artifact == 'log' or '_log' in artifact:
                    logs[artifact] = value
        elif node.get('parent'):
            parent = self._api.node.get(node['parent'])
            if parent:
                logs = self._collect_logs(parent)
        return logs

    def _collect_errors(self, node):
        """Returns a dict containing the 'error_code' and 'error_msg'
        data fields of <node>. If <node> doesn't have any info in them,
        it searches upwards through parent nodes until it reaches a node
        that has them.
        """
        if node['data'].get('error_code'):
            return {
                'error_code': node['data']['error_code'],
                'error_msg': node['data']['error_msg']
            }
        elif node.get('parent'):
            parent = self._api.node.get(node['parent'])
            return self._collect_errors(parent)
        return {
            'error_code': None,
            'error_msg': None
        }

    def _create_regression(self, failed_node, last_pass_node):
        """Method to create a regression"""
        regression = {}
        regression['kind'] = 'regression'
        # Make regression "active" by default.
        # TODO: 'result' is currently optional in the model, so we set
        # it here. Remove this line if the field is set as mandatory in
        # the future.
        regression['result'] = 'fail'
        regression['name'] = failed_node['name']
        regression['path'] = failed_node['path']
        regression['group'] = failed_node['group']
        regression['state'] = 'done'
        error = self._collect_errors(failed_node)
        regression['data'] = {
            'fail_node': failed_node['id'],
            'pass_node': last_pass_node['id'],
            'arch': failed_node['data'].get('arch'),
            'defconfig': failed_node['data'].get('defconfig'),
            'config_full': failed_node['data'].get('config_full'),
            'compiler': failed_node['data'].get('compiler'),
            'platform': failed_node['data'].get('platform'),
            'device': failed_node['data'].get('device'),
            'failed_kernel_version': failed_node['data'].get('kernel_revision'),   # noqa
            'error_code': error['error_code'],
            'error_msg': error['error_msg'],
            'node_sequence': [],
        }
        regression['artifacts'] = self._collect_logs(failed_node)
        return regression

    def _get_last_matching_node(self, search_params):
        """Returns the last (by creation date) occurrence of a node
        matching a set of search parameters, or None if no nodes were
        found.

        TODO: Move this to core helpers.

        """
        # Workaround: Don't use 'path' as a search parameter (we can't
        # use lists as query parameter values). Instead, do the
        # filtering in python code
        path = search_params.pop('path')
        nodes = self._api.node.find(search_params)
        nodes = [node for node in nodes if node['path'] == path]
        if not nodes:
            return None
        node = sorted(
            nodes,
            key=lambda node: node['created'],
            reverse=True
        )[0]
        return node

    def _get_related_regression(self, node):
        """Returns the last active regression that points to the same job
        run instance. Returns None if no active regression was found.

        """
        search_params = {
            'kind': 'regression',
            'result': 'fail',
            'name': node['name'],
            'group': node['group'],
            'path': node['path'],
            'data.failed_kernel_version.tree': node['data']['kernel_revision']['tree'],
            'data.failed_kernel_version.branch': node['data']['kernel_revision']['branch'],
            'data.failed_kernel_version.url': node['data']['kernel_revision']['url'],
            'created__lt': node['created'],
            # Parameters that may be null in some test nodes
            'data.arch': node['data'].get('arch', 'null'),
            'data.defconfig': node['data'].get('defconfig', 'null'),
            'data.config_full': node['data'].get('config_full', 'null'),
            'data.compiler': node['data'].get('compiler', 'null'),
            'data.platform': node['data'].get('platform', 'null')
        }
        return self._get_last_matching_node(search_params)

    def _get_previous_job_instance(self, node):
        """Returns the previous job run instance of <node>, or None if
        no one was found.

        """
        search_params = {
            'kind': node['kind'],
            'name': node['name'],
            'group': node['group'],
            'path': node['path'],
            'data.kernel_revision.tree': node['data']['kernel_revision']['tree'],
            'data.kernel_revision.branch': node['data']['kernel_revision']['branch'],
            'data.kernel_revision.url': node['data']['kernel_revision']['url'],
            'created__lt': node['created'],
            'state': 'done',
            # Parameters that may be null in some test nodes
            'data.arch': node['data'].get('arch', 'null'),
            'data.defconfig': node['data'].get('defconfig', 'null'),
            'data.config_full': node['data'].get('config_full', 'null'),
            'data.compiler': node['data'].get('compiler', 'null'),
            'data.platform': node['data'].get('platform', 'null'),
        }
        return self._get_last_matching_node(search_params)

    def _process_node(self, node):
        if node['result'] == 'pass':
            # Find existing active regression
            regression = self._get_related_regression(node)
            if regression:
                # Set regression as inactive
                regression['data']['node_sequence'].append(node['id'])
                regression['result'] = 'pass'
                self._api.node.update(regression)
        elif node['result'] == 'fail':
            previous = self._get_previous_job_instance(node)
            if not previous:
                # Not a regression, since there's no previous test run
                pass
            elif previous['result'] == 'pass':
                self.log.info(f"Detected regression for node id: {node['id']}")
                # Skip the regression generation if it was already in the
                # DB. This may happen if a job was detected to generate a
                # regression when it failed and then the same job was
                # checked again after its parent job finished running and
                # was updated.
                existing_regression = self._api.node.find({
                    'kind': 'regression',
                    'result': 'fail',
                    'data.fail_node': node['id'],
                    'data.pass_node': previous['id']
                })
                if not existing_regression:
                    regression = self._create_regression(node, previous)
                    resp = self._api_helper.submit_regression(regression)
                    reg = json.loads(resp.text)
                    self.log.info(f"Regression submitted: {reg['id']}")
                    # Update node
                    node['data']['regression'] = reg['id']
                    self._api.node.update(node)
                else:
                    self.log.info(f"Skipping regression: already exists")
            elif previous['result'] == 'fail':
                # Find existing active regression
                regression = self._get_related_regression(node)
                if regression:
                    if node['id'] in regression['data']['node_sequence']:
                        # The node is already in an active
                        # regression. This may happen if the job was
                        # processed right after it finished and then
                        # again after its parent job finished and was
                        # updated
                        return
                    # Update active regression
                    regression['data']['node_sequence'].append(node['id'])
                    self._api.node.update(regression)
                    # Update node
                    node['data']['regression'] = regression['id']
                    self._api.node.update(node)
        # Process children recursively:
        # When a node hierarchy is submitted on a single operation,
        # an event is generated only for the root node. Walk the
        # children node tree to check for event-less finished jobs
        child_nodes = self._api.node.find({'parent': node['id']})
        if child_nodes:
            for child_node in child_nodes:
                self._process_node(child_node)

    def _run(self, sub_id):
        """Method to run regression detection and generation"""
        self.log.info("Tracking regressions... ")
        self.log.info("Press Ctrl-C to stop.")
        sys.stdout.flush()
        while True:
            node, _ = self._api_helper.receive_event_node(sub_id)
            if node['kind'] == 'checkout' or node['kind'] == 'regression':
                continue
            self._process_node(node)
        return True


class cmd_run(Command):
    help = "Regression tracker"
    args = [Args.api_config]

    def __call__(self, configs, args):
        return RegressionTracker(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('regression_tracker', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
