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
            'failed_kernel_version': failed_node['data'].get('kernel_revision'),   # noqa
            'error_code': error['error_code'],
            'error_msg': error['error_msg'],
        }
        regression['artifacts'] = self._collect_logs(failed_node)
        resp = self._api_helper.submit_regression(regression)
        reg = json.loads(resp.text)
        self.log.info(f"Regression submitted: {reg['id']}")

    def _detect_regression(self, fail_node):
        """Detects if <fail_node> (a failed job) produces a regression,
        ie. if the previous job run with the same parameters passed"""
        search_params = {
            'name': fail_node['name'],
            'group': fail_node['group'],
            'path': fail_node['path'],
            'data.kernel_revision.tree':
                fail_node['data']['kernel_revision']['tree'],
            'data.kernel_revision.branch':
                fail_node['data']['kernel_revision']['branch'],
            'data.kernel_revision.url':
                fail_node['data']['kernel_revision']['url'],
            'created__lt': fail_node['created'],
            'state': 'done',
            # Parameters that may be null in some test nodes
            'data.arch': fail_node['data'].get('arch', 'null'),
            'data.defconfig': fail_node['data'].get('defconfig', 'null'),
            'data.config_full': fail_node['data'].get('config_full', 'null'),
            'data.compiler': fail_node['data'].get('compiler', 'null'),
            'data.platform': fail_node['data'].get('platform', 'null'),
        }
        previous_nodes = self._api.node.find(search_params)
        if not previous_nodes:
            return
        previous_node = sorted(
            previous_nodes,
            key=lambda node: node['created'],
            reverse=True
        )[0]
        if previous_node['result'] == 'pass':
            self.log.info("Detected regression for node id: "
                          f"{fail_node['id']}")
            # Skip the regression generation if it was already in the
            # DB. This may happen if a job was detected to generate a
            # regression when it failed and then the same job was
            # checked again after its parent job finished running and
            # was updated.
            existing_regression = self._api.node.find({
                'kind': 'regression',
                'data.fail_node': fail_node['id'],
                'data.pass_node': previous_node['id']
            })
            if not existing_regression:
                self._create_regression(fail_node, previous_node)
            else:
                self.log.info(f"Skipping regression: already exists")

    def _get_all_failed_child_nodes(self, failures, root_node):
        """Method to get all failed nodes recursively from top-level node"""
        child_nodes = self._api.node.find({'parent': root_node['id']})
        for node in child_nodes:
            if node['result'] == 'fail':
                failures.append(node)
            self._get_all_failed_child_nodes(failures, node)

    def _run(self, sub_id):
        """Method to run regression detection and generation"""
        self.log.info("Tracking regressions... ")
        self.log.info("Press Ctrl-C to stop.")
        sys.stdout.flush()
        while True:
            node = self._api_helper.receive_event_node(sub_id)
            if node['kind'] == 'checkout':
                continue
            if node['result'] == 'fail':
                self._detect_regression(node)
            # When a node hierarchy is submitted on a single operation,
            # an event is generated only for the root node. Walk the
            # children node tree to check for event-less failed jobs
            failures = []
            self._get_all_failed_child_nodes(failures, node)
            for node in failures:
                self._detect_regression(node)
            sys.stdout.flush()
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
