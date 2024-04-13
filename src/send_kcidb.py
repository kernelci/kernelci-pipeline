#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Maryam Yusuf
# Author: Maryam Yusuf <maryam.m.yusuf1802@gmail.com>
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

"""KCIDB bridge service"""

import datetime
import sys

import kernelci
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts
import kcidb

from base import Service


class KCIDBBridge(Service):
    def __init__(self, configs, args, name):
        super().__init__(configs, args, name)

    def _setup(self, args):
        return {
            'client': kcidb.Client(
                project_id=args.kcidb_project_id,
                topic_name=args.kcidb_topic_name
            ),
            'sub_id': self._api_helper.subscribe_filters({
                'kind': 'checkout',
                'state': 'done',
            }),
            'origin': args.origin,
        }

    def _stop(self, context):
        if context['sub_id']:
            self._api_helper.unsubscribe_filters(context['sub_id'])

    def _remove_none_fields(self, data):
        """Remove all keys with `None` values as KCIDB doesn't allow it"""
        if isinstance(data, dict):
            return {key: self._remove_none_fields(val)
                    for key, val in data.items() if val is not None}
        if isinstance(data, list):
            return [self._remove_none_fields(item) for item in data]
        return data

    def _send_revision(self, client, revision):
        revision = self._remove_none_fields(revision)
        self.log.debug(f"DEBUG: sending revision: {revision}")
        if kcidb.io.SCHEMA.is_valid(revision):
            return client.submit(revision)
        self.log.error("Aborting, invalid data")
        try:
            kcidb.io.SCHEMA.validate(revision)
        except Exception as exc:
            self.log.error(f"Validation error: {str(exc)}")

    @staticmethod
    def _set_timezone(created_timestamp):
        created_time = datetime.datetime.fromisoformat(created_timestamp)
        if not created_time.tzinfo:
            tz_utc = datetime.timezone(datetime.timedelta(hours=0))
            created_time = datetime.datetime.fromtimestamp(
                created_time.timestamp(), tz=tz_utc)
        return created_time.isoformat()

    def _parse_build_nodes(self, origin, build_nodes):
        parsed_build_nodes = []
        for node in build_nodes:
            parsed_node = {
                'id': f"{origin}:{node['id']}",
                'checkout_id': f"{origin}:{node['parent']}",
                'comment': node['data']['kernel_revision'].get('describe'),
                'origin': origin,
                'architecture': node['data'].get('arch'),
                'compiler': node['data'].get('compiler'),
                'config_name': node['data'].get('config_full'),
                'start_time': self._set_timezone(node['created']),
                'log_url': node.get('artifacts', {}).get('build_log'),
                'valid': node['result'] == 'pass',
            }
            parsed_build_nodes.append(parsed_node)
        return parsed_build_nodes

    def _parse_checkout_node(self, origin, checkout_node):
        return {
            'id': f"{origin}:{checkout_node['id']}",
            'origin': origin,
            'tree_name': checkout_node['data']['kernel_revision']['tree'],
            'git_repository_url':
                checkout_node['data']['kernel_revision']['url'],
            'git_commit_hash':
                checkout_node['data']['kernel_revision']['commit'],
            'git_commit_name':
                checkout_node['data']['kernel_revision'].get('describe'),
            'git_repository_branch':
                checkout_node['data']['kernel_revision']['branch'],
            'start_time': self._set_timezone(checkout_node['created']),
            'patchset_hash': '',
            'misc': {
                'submitted_by': 'kernelci-pipeline'
            },
        }

    def _parse_node_path(self, path):
        """Parse and create KCIDB schema compatible node path

        Convert node path list to dot-separated string and exclude
        'checkout' from the path to make test suite the top level node
        """
        if isinstance(path, list):
            path_str = '.'.join(path[1:])
            # Replace whitespace with "_" to match the allowed pattern for
            # test `path` i.e '^[.a-zA-Z0-9_-]*$'
            return path_str.replace(" ", "_")
        return None

    def _parse_node_result(self, test_node):
        if test_node['result'] == 'incomplete':
            if test_node['data'].get('error_code') in ('submit_error', 'invalid_job_params'):
                return 'MISS'
            return 'ERROR'
        return test_node['result'].upper()

    def _parse_test_node(self, origin, test_node, build_id, parsed_test_nodes):
        parsed_test_node = {
            'id': f"{origin}:{test_node['id']}",
            'origin': origin,
            'build_id': f"{origin}:{build_id}",
            'comment': f"{test_node['name']} on {test_node['data'].get('platform')} \
in {test_node['data'].get('runtime')}",
            'start_time': self._set_timezone(test_node['created']),
            'environment': {
                'comment': f"Runtime: {test_node['data'].get('runtime')}",
                'misc': {
                    'platform': test_node['data'].get('platform'),
                    'job_id': test_node['data'].get('job_id'),
                    'job_context': test_node['data'].get('job_context'),
                }
            },
            'waived': False,
            'path': self._parse_node_path(test_node['path']),
        }
        if test_node['result']:
            parsed_test_node['status'] = self._parse_node_result(test_node)
        parsed_test_nodes.append(parsed_test_node)

    def _get_test_nodes(self, origin, parent_node_id, build_id, parsed_test_nodes):
        child_nodes = self._api.node.find({
            'parent': parent_node_id
        })

        for child_node in child_nodes:
            if child_node['kind'] == 'test':
                self._parse_test_node(origin, child_node, build_id,
                                      parsed_test_nodes)
            test_nodes = self._api.node.find({
                'kind': 'test',
                'parent': child_node['id']
            })

            for test_node in test_nodes:
                self._parse_test_node(origin, test_node, build_id,
                                      parsed_test_nodes)
                self._get_test_nodes(origin, test_node['id'], build_id,
                                     parsed_test_nodes)

    def _get_tests_for_build(self, origin, build_nodes, parsed_test_nodes):
        for build_node in build_nodes:
            build_id = build_node['id']
            self._get_test_nodes(origin, build_id, build_id,
                                 parsed_test_nodes)

    def _parse_tests_for_dummy_build(self, origin, parent_node_id, build_id,
                                     parsed_test_nodes):
        checkout_test_nodes = self._api.node.find({
                'kind': 'test',
                'parent': parent_node_id
            })
        for test_node in checkout_test_nodes:
            self._parse_test_node(origin, test_node, build_id, parsed_test_nodes)
            child_test_nodes = self._api.node.find({
                'kind': 'test',
                'parent': test_node['id']
            })
            for child_test_node in child_test_nodes:
                self._parse_test_node(origin, child_test_node, build_id,
                                      parsed_test_nodes)
                self._parse_tests_for_dummy_build(origin, child_test_node['id'],
                                                  build_id, parsed_test_nodes)

    def _get_dummy_build_node(self, origin, checkout_node):
        return {
            'id': f"{origin}:dummy_{checkout_node['id']}",
            'checkout_id': f"{origin}:{checkout_node['id']}",
            'comment': checkout_node['data']['kernel_revision'].get('describe'),
            'origin': origin,
            'start_time': self._set_timezone(checkout_node['created']),
            'valid': True,
        }

    def _run(self, context):
        self.log.info("Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            checkout_node = self._api_helper.receive_event_node(context['sub_id'])
            self.log.info(f"Submitting node to KCIDB: {checkout_node['id']}")

            build_nodes = self._api.node.find({
                'parent': checkout_node['id'],
                'kind': 'kbuild'
            })
            parsed_test_nodes = []
            parsed_build_nodes = self._parse_build_nodes(context['origin'], build_nodes)
            if build_nodes:
                self._get_tests_for_build(context['origin'], build_nodes,
                                          parsed_test_nodes)

            checkout_test_nodes_count = self._api.node.count({
                'parent': checkout_node['id'],
                'kind': 'test'
            })

            if checkout_test_nodes_count:
                # Create a dummy build node for tests hanging directly from
                # checkout node and use it for the test nodes
                dummy_build_node = self._get_dummy_build_node(context['origin'],
                                                              checkout_node)
                parsed_build_nodes.append(dummy_build_node)
                build_id = dummy_build_node['id'].split(":")[1]
                self._parse_tests_for_dummy_build(context['origin'],
                                                  checkout_node['id'],
                                                  build_id,
                                                  parsed_test_nodes)

            revision = {
                'checkouts': [
                    self._parse_checkout_node(context['origin'], checkout_node)
                ],
                'builds': parsed_build_nodes,
                'tests': parsed_test_nodes,
                'version': {
                    'major': 4,
                    'minor': 3
                }
            }
            self._send_revision(context['client'], revision)
        return True


class cmd_run(Command):
    help = "Listen for events and send them to KCDIB"
    args = [
        Args.api_config,
        {
            'name': '--kcidb-topic-name',
            'help': "KCIDB topic name",
        },
        {
            'name': '--kcidb-project-id',
            'help': "KCIDB project ID",
        },
        {
            'name': '--origin',
            'help': "CI system identifier",
        },
    ]

    def __call__(self, configs, args):
        return KCIDBBridge(configs, args, 'send_kcidb').run(args)


if __name__ == '__main__':
    opts = parse_opts('send_kcidb', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
