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
import re
import io
import gzip
import requests

import kernelci
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts
import kcidb

from base import Service


MISSED_TEST_CODES = (
    'Bug',
    'Configuration',
    'Infrastructure',
    'invalid_job_params',
    'Job',
    'job_generation_error',
    'ObjectNotPersisted',
    'RequestBodyTooLarge',
    'submit_error',
    'Unexisting permission codename.',
)

ERRORED_TEST_CODES = (
    'Canceled',
    'LAVATimeout',
    'MultinodeTimeout',
    'node_timeout',
    'Test',
)


class KCIDBBridge(Service):
    def __init__(self, configs, args, name):
        super().__init__(configs, args, name)
        self._jobs = configs['jobs']

    def _setup(self, args):
        return {
            'client': kcidb.Client(
                project_id=args.kcidb_project_id,
                topic_name=args.kcidb_topic_name
            ),
            'sub_id': self._api_helper.subscribe_filters({
                'state': ('done', 'available'),
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
        if any(value for key, value in revision.items() if key != 'version'):
            self.log.debug(f"DEBUG: sending revision: {revision}")
            if kcidb.io.SCHEMA.is_valid(revision):
                client.submit(revision)
            else:
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

    def _parse_checkout_node(self, origin, checkout_node):
        result = checkout_node.get('result')
        result_map = {
            'pass': True,
            'fail': False,
            'incomplete': False,
        }
        valid = result_map[result] if result else None
        return [{
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
            'valid': valid,
        }]

    def _get_output_files(self, artifacts: dict, exclude_properties=None):
        output_files = []
        for name, url in artifacts.items():
            if exclude_properties and name in exclude_properties:
                continue
            # Replace "/" with "_" to match with the allowed pattern
            # for "name" property of "output_files" i.e. '^[^/]+$'
            name = name.replace("/", "_")
            output_files.append(
                {
                    'name': name,
                    'url': url
                }
            )
        return output_files

    def _get_log_excerpt(self, log_url):
        """Parse compressed(gzip) or text log file and return last 16*1024 characters as it's
        the maximum allowed length for KCIDB `log_excerpt` field"""
        try:
            res = requests.get(log_url, timeout=60)
            if res.status_code != 200:
                return None
        except requests.exceptions.ConnectionError as exc:
            self.log.error(f"{str(exc)}")
            return None

        try:
            # parse compressed file such as lava log files
            buffer_data = io.BytesIO(res.content)
            with gzip.open(buffer_data, mode='rt') as fp:
                data = fp.read()
                return data[-(16*1024):]
        except gzip.BadGzipFile:
            # parse text file such as kunit log file `test_log`
            data = res.content.decode("utf-8")
            return data[-(16*1024):]

    def _parse_build_node(self, origin, node):
        parsed_build_node = {
            'checkout_id': f"{origin}:{node['parent']}",
            'id': f"{origin}:{node['id']}",
            'origin': origin,
            'comment': node['data']['kernel_revision'].get('describe'),
            'start_time': self._set_timezone(node['created']),
            'architecture': node['data'].get('arch'),
            'compiler': node['data'].get('compiler'),
            'config_name': node['data'].get('defconfig'),
            'valid': node['result'] == 'pass',
            'misc': {
                'platform': node['data'].get('platform'),
                'runtime': node['data'].get('runtime'),
                'job_id': node['data'].get('job_id'),
                'job_context': node['data'].get('job_context'),
                'kernel_type': node['data'].get('kernel_type'),
                'error_code': node['data'].get('error_code'),
                'error_msg': node['data'].get('error_msg'),
            }
        }
        artifacts = node.get('artifacts')
        if artifacts:
            parsed_build_node['output_files'] = self._get_output_files(
                artifacts=artifacts,
                exclude_properties=('build_log', '_config')
            )
            parsed_build_node['config_url'] = artifacts.get('_config')
            parsed_build_node['log_url'] = artifacts.get('build_log')
            log_url = parsed_build_node['log_url']
            if log_url:
                parsed_build_node['log_excerpt'] = self._get_log_excerpt(
                    log_url)

        return [parsed_build_node]

    def _replace_restricted_chars(self, path, pattern, replace_char='_'):
        # Replace restricted characters with "_" to match the allowed pattern
        new_path = ""
        for char in path:
            if not re.match(pattern, char):
                new_path += replace_char
            else:
                new_path += char
        return new_path

    def _parse_node_path(self, path, is_checkout_child):
        """Parse and create KCIDB schema compatible node path
        Convert node path list to dot-separated string. Use unified
        test suite name to exclude build and runtime information
        from the test path.
        For example, test path ['checkout', 'kbuild-gcc-10-x86', 'baseline-x86']
        would be converted to "boot"
        """
        if isinstance(path, list):
            if is_checkout_child:
                # nodes with path such as ['checkout', 'kver']
                parsed_path = path[1:]
            else:
                # nodes with path such as ['checkout', 'kbuild-gcc-10-x86', 'baseline-x86']
                parsed_path = path[2:]
                # Handle node with path ['checkout', 'kbuild-gcc-10-x86', 'sleep', 'sleep']
                if len(parsed_path) >= 2:
                    if parsed_path[0] == parsed_path[1]:
                        parsed_path = parsed_path[1:]
            new_path = []
            for sub_path in parsed_path:
                if sub_path in self._jobs:
                    suite_name = self._jobs[sub_path].kcidb_test_suite
                    if suite_name:
                        new_path.append(suite_name)
                    else:
                        self.log.error(f"KCIDB test suite mapping not found for \
the test: {sub_path}")
                        return None
                else:
                    new_path.append(sub_path)
            # Handle path such as ['tast-ui-x86-intel', 'tast', 'os-release'] converted
            # to ['tast', 'tast', 'os-release']
            if len(new_path) >= 2:
                if new_path[0] == new_path[1]:
                    new_path = new_path[1:]
            path_str = '.'.join(new_path)
            # Allowed pattern for test path is ^[.a-zA-Z0-9_-]*$'
            formatted_path_str = self._replace_restricted_chars(path_str, r'^[.a-zA-Z0-9_-]*$')
            return formatted_path_str if formatted_path_str else None
        return None

    def _parse_node_result(self, test_node):
        if test_node['result'] == 'incomplete':
            error_code = test_node['data'].get('error_code')
            if error_code in ERRORED_TEST_CODES:
                return 'ERROR'
            if error_code in MISSED_TEST_CODES:
                return 'MISS'
        return test_node['result'].upper()

    def _get_parent_build_node(self, node):
        node = self._api.node.get(node['parent'])
        if node['kind'] == 'kbuild' or node['kind'] == 'checkout':
            return node
        return self._get_parent_build_node(node)

    def _create_dummy_build_node(self, origin, checkout_node, arch):
        return {
            'id': f"{origin}:dummy_{checkout_node['id']}_{arch}" if arch
                  else f"{origin}:dummy_{checkout_node['id']}",
            'checkout_id': f"{origin}:{checkout_node['id']}",
            'comment': 'Dummy build for tests hanging from checkout',
            'origin': origin,
            'start_time': self._set_timezone(checkout_node['created']),
            'valid': True,
            'architecture': arch,
        }

    def _get_artifacts(self, node):
        """Retrive artifacts
        Get node artifacts. If the node doesn't have the artifacts,
        it will search through parent nodes recursively until
        it's found.
        """
        artifacts = node.get('artifacts')
        if not artifacts:
            if node.get('parent'):
                parent = self._api.node.get(node['parent'])
                if parent:
                    artifacts = self._get_artifacts(parent)
        return artifacts

    def _get_job_metadata(self, node):
        """Retrive job metadata
        Get job metadata such as job ID and context. If the node doesn't
        have the metadata, it will search through parent nodes recursively
        until it's found.
        """
        data = node.get('data')
        if not data.get('job_id'):
            if node.get('parent'):
                parent = self._api.node.get(node['parent'])
                if parent:
                    data = self._get_job_metadata(parent)
        return data

    def _get_error_metadata(self, node):
        """Retrive error metadata for failed tests
        Get error metadata such as error code and message for failed jobs.
        If the node doesn't have the metadata, it will search through parent
        nodes recursively until it's found.
        """
        data = node.get('data')
        if not data.get('error_code'):
            if node.get('parent'):
                parent = self._api.node.get(node['parent'])
                if parent:
                    data = self._get_error_metadata(parent)
        return data

    def _parse_test_node(self, origin, test_node):
        dummy_build = {}
        is_checkout_child = False
        build_node = self._get_parent_build_node(test_node)
        # Create dummy build node if test is hanging directly from checkout
        if build_node['kind'] == 'checkout':
            is_checkout_child = True
            dummy_build = self._create_dummy_build_node(origin, build_node,
                                                        test_node['data'].get('arch'))
            build_id = dummy_build['id']
        else:
            build_id = f"{origin}:{build_node['id']}"

        parsed_test_node = {
            'build_id': build_id,
            'id': f"{origin}:{test_node['id']}",
            'origin': origin,
            'comment': f"{test_node['name']} on {test_node['data'].get('platform')} \
in {test_node['data'].get('runtime')}",
            'start_time': self._set_timezone(test_node['created']),
            'environment': {
                'comment': f"Runtime: {test_node['data'].get('runtime')}",
                'misc': {
                    'platform': test_node['data'].get('platform'),
                }
            },
            'waived': False,
            'path': self._parse_node_path(test_node['path'], is_checkout_child),
            'misc': {
                'test_source': test_node['data'].get('test_source'),
                'test_revision': test_node['data'].get('test_revision'),
                'compiler': test_node['data'].get('compiler'),
                'kernel_type': test_node['data'].get('kernel_type'),
                'arch': test_node['data'].get('arch'),
            }
        }

        if test_node['result']:
            parsed_test_node['status'] = self._parse_node_result(test_node)
            if parsed_test_node['status'] == 'SKIP':
                # No artifacts and metadata will be available for skipped tests
                return parsed_test_node, dummy_build

        job_metadata = self._get_job_metadata(test_node)
        if job_metadata:
            parsed_test_node['environment']['misc']['job_id'] = job_metadata.get(
                'job_id')
            parsed_test_node['environment']['misc']['job_context'] = job_metadata.get(
                'job_context')

        artifacts = self._get_artifacts(test_node)
        if artifacts:
            parsed_test_node['output_files'] = self._get_output_files(
                artifacts=artifacts,
                exclude_properties=('lava_log', 'test_log')
            )
            if artifacts.get('lava_log'):
                parsed_test_node['log_url'] = artifacts.get('lava_log')
            else:
                parsed_test_node['log_url'] = artifacts.get('test_log')

            log_url = parsed_test_node['log_url']
            if log_url:
                parsed_test_node['log_excerpt'] = self._get_log_excerpt(
                    log_url)

        if test_node['result'] != 'pass':
            error_metadata = self._get_error_metadata(test_node)
            if error_metadata:
                parsed_test_node['misc']['error_code'] = error_metadata.get(
                    'error_code')
                parsed_test_node['misc']['error_msg'] = error_metadata.get(
                    'error_msg')

        return parsed_test_node, dummy_build

    def _get_test_data(self, node, origin,
                       parsed_test_node, parsed_build_node):
        test_node, build_node = self._parse_test_node(
            origin, node
        )
        if not test_node['path']:
            self.log.info(f"Not sending test as path information is missing: {test_node['id']}")
            return

        if 'setup' in test_node.get('path'):
            # do not send setup tests
            return

        parsed_test_node.append(test_node)
        if build_node:
            parsed_build_node.append(build_node)

    def _get_test_data_recursively(self, node, origin, parsed_test_node, parsed_build_node):
        child_nodes = self._api.node.find({'parent': node['id']})
        if not child_nodes:
            self._get_test_data(node, origin, parsed_test_node,
                                parsed_build_node)
        else:
            for child in child_nodes:
                self._get_test_data_recursively(child, origin, parsed_test_node,
                                                parsed_build_node)

    def _run(self, context):
        self.log.info("Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            node, is_hierarchy = self._api_helper.receive_event_node(context['sub_id'])
            self.log.info(f"Received an event for node: {node['id']}")

            parsed_checkout_node = []
            parsed_build_node = []
            parsed_test_node = []

            if node['kind'] == 'checkout':
                parsed_checkout_node = self._parse_checkout_node(
                    context['origin'], node)

            elif node['kind'] == 'kbuild':
                parsed_build_node = self._parse_build_node(
                    context['origin'], node
                )

            elif node['kind'] == 'test':
                self._get_test_data(node, context['origin'],
                                    parsed_test_node, parsed_build_node)

            elif node['kind'] == 'job':
                # Send only failed/incomplete job nodes
                if node['result'] != 'pass':
                    self._get_test_data(node, context['origin'],
                                        parsed_test_node, parsed_build_node)
                if is_hierarchy:
                    self._get_test_data_recursively(node, context['origin'],
                                                    parsed_test_node, parsed_build_node)

            revision = {
                'checkouts': parsed_checkout_node,
                'builds': parsed_build_node,
                'tests': parsed_test_node,
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
