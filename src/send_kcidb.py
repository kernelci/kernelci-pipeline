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
from urllib.parse import urljoin
import requests
import time
import hashlib
import os

import kernelci
import kernelci.config
from kernelci.config.runtime import RuntimeLAVA
from kernelci.legacy.cli import Args, Command, parse_opts
import kcidb
from kernelci_pipeline.logspec_api import generate_issues_and_incidents

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
    'kbuild_internal_error',
)

ERRORED_TEST_CODES = (
    'Canceled',
    'LAVATimeout',
    'MultinodeTimeout',
    'Test',
)


class KCIDBBridge(Service):
    def __init__(self, configs, args, name):
        super().__init__(configs, args, name)
        self._jobs = configs['jobs']
        self._platforms = configs['platforms']
        self._lava_labs = {}
        self._last_unprocessed_search = None
        self._nodecache = {}
        self._excerptcache = {}
        for runtime_name, runtime_configs in configs['runtimes'].items():
            if isinstance(runtime_configs, RuntimeLAVA):
                self._lava_labs[runtime_name] = runtime_configs.url
        self._current_user = self._api.user.whoami()

    def _setup(self, args):
        db_conn = (
            f"postgresql:dbname={args.database_name} "
            f"user={args.postgresql_user} host={args.postgresql_host} "
            f"password={args.postgresql_password} "
            f"port={args.postgresql_port}"
        )
        db_client = kcidb.db.Client(db_conn)
        return {
            'client': kcidb.Client(
                project_id=args.kcidb_project_id,
                topic_name=args.kcidb_topic_name
            ),
            'kcidb_oo_client': kcidb.oo.Client(db_client),
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

    def _print_debug(self, data):
        """Print debug information for the data being sent to KCIDB"""
        fields = ['checkouts', 'builds', 'tests', 'issues', 'incidents']
        for field in fields:
            if field in data:
                for item in data[field]:
                    id = item.get('id')
                    self.log.debug(f"Sending to KCIDB: {field}: {id}")

    def _send_revision(self, client, revision):
        revision = self._remove_none_fields(revision)
        if any(value for key, value in revision.items() if key != 'version'):
            # remove log_excerpt field, as it is filling up the logs
            self._print_debug(revision)
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

        # Don't send "timed-out" checkout node to KCIDB
        if result == 'incomplete' and \
                checkout_node['data'].get('error_code') == 'node_timeout':
            return []

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
            'git_commit_tags':
                checkout_node['data']['kernel_revision'].get('commit_tags'),
            'git_commit_message':
                checkout_node['data']['kernel_revision'].get('commit_message'),
            'git_repository_branch_tip':
                checkout_node['data']['kernel_revision'].get('tip_of_branch'),
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
        # is log_url in cache?
        if log_url in self._excerptcache:
            return self._excerptcache[log_url]
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
                trunc_data = data[-(16*1024):]
                self._excerptcache[log_url] = trunc_data
                return trunc_data
        except gzip.BadGzipFile:
            # parse text file such as kunit log file `test_log`
            data = res.content.decode("utf-8")
            trunc_data = data[-(16*1024):]
            self._excerptcache[log_url] = trunc_data
            return trunc_data

    def _cache_expire(self):
        """Read list of files /tmp/cached_* and remove the oldest one
        if there is more than 100 files"""
        cached_files = [os.path.join("/tmp", f) for f in
                        os.listdir("/tmp") if f.startswith("cached_")]
        if len(cached_files) > 100:
            oldest_file = min(cached_files, key=os.path.getctime)
            os.remove(f"{oldest_file}")

    def _cached_fetch(self, url):
        """
        We fetch URL to /tmp/cached_HASH and return the path to the file
        HASH is sha256 of the URL
        Also if we have more than 100 files we remove the oldest one
        """
        hash = hashlib.sha256(url.encode()).hexdigest()
        path = f"/tmp/cached_{hash}"
        if os.path.exists(path):
            return path
        # Fetch the URL
        self._cache_expire()
        try:
            res = requests.get(url, timeout=60)
            if res.status_code != 200:
                return None
        except requests.exceptions.ConnectionError as exc:
            self.log.error(f"Retrieving file failed: {str(exc)}")
            return None
        with open(path, "wb") as f:
            f.write(res.content)
        return path

    def _parse_build_node(self, origin, node):
        result = node.get('result')
        error_code = node['data'].get('error_code')

        # Skip timed-out build node submission
        if result == 'incomplete' and error_code == 'node_timeout':
            return []

        result_map = {
            'pass': True,
            'fail': False,
            'incomplete': None,
        }
        valid = result_map.get(result) if result else None

        parsed_build_node = {
            'checkout_id': f"{origin}:{node['parent']}",
            'id': f"{origin}:{node['id']}",
            'origin': origin,
            'comment': node['data']['kernel_revision'].get('describe'),
            'start_time': self._set_timezone(node['created']),
            'architecture': node['data'].get('arch'),
            'compiler': node['data'].get('compiler'),
            'config_name': node['data'].get('defconfig'),
            'valid': valid,
            'misc': {
                'platform': node['data'].get('platform'),
                'runtime': node['data'].get('runtime'),
                'job_id': node['data'].get('job_id'),
                'job_context': node['data'].get('job_context'),
                'kernel_type': node['data'].get('kernel_type'),
                'error_code': error_code,
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

    def _get_node_cached(self, node_id):
        if node_id in self._nodecache:
            return self._nodecache[node_id]
        else:
            node = self._api.node.get(node_id)
            if node:
                self._nodecache[node_id] = node
                return node

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
            self.log.debug(f"Error code is not set for {test_node['id']}")
            return None
        return test_node['result'].upper()

    def _get_parent_build_node(self, node):
        node = self._get_node_cached(node['parent'])
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
                parent = self._get_node_cached(node['parent'])
                if parent:
                    artifacts = self._get_artifacts(parent)
        return artifacts

    def _get_job_metadata(self, node):
        """Retrive job metadata
        Get job metadata such as job ID, context, and job URL (for lava jobs).
        If the node doesn't have the metadata, it will search through parent nodes
        recursively until it's found.
        """
        data = node.get('data')
        if not data.get('job_id'):
            if node.get('parent'):
                parent = self._get_node_cached(node['parent'])
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
                parent = self._get_node_cached(node['parent'])
                if parent:
                    data = self._get_error_metadata(parent)
        return data

    def _parse_test_node(self, origin, test_node):
        # We submit incomplete test nodes to KCIDB
        # As they might have useful information

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

        platform = test_node['data'].get('platform')
        compatible = None
        if platform:
            platformobj = self._platforms.get(platform)
            if platformobj:
                compatible = platformobj.compatible
            else:
                self.log.error(f"Platform {platform} not found in the platform list")

        runtime = test_node['data'].get('runtime')
        misc = test_node['data'].get('misc')
        parsed_test_node = {
            'build_id': build_id,
            'id': f"{origin}:{test_node['id']}",
            'origin': origin,
            'comment': f"{test_node['name']} on {platform} \
in {runtime}",
            'start_time': self._set_timezone(test_node['created']),
            'environment': {
                'comment': f"Runtime: {runtime}",
                'compatible': compatible,
                'misc': {
                    'platform': platform,
                    'measurement': misc.get('measurement') if misc else None
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
                'runtime': runtime,
            }
        }

        if test_node['result']:
            parsed_test_node['status'] = self._parse_node_result(test_node)
            if parsed_test_node['status'] == 'SKIP':
                # No artifacts and metadata will be available for skipped tests
                return parsed_test_node, dummy_build

        job_metadata = self._get_job_metadata(test_node)
        if job_metadata:
            lab_url = self._lava_labs.get(job_metadata.get('runtime'))
            if lab_url:
                job_url = urljoin(lab_url, f"/scheduler/job/{job_metadata.get('job_id')}")
                parsed_test_node['environment']['misc']['job_url'] = job_url
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
        if not test_node:
            return

        if not test_node['path']:
            self.log.info(f"Not sending test as path information is missing: {test_node['id']}")
            return

        path = test_node.get('path')
        if 'setup' in path and 'os-release' not in path:
            # do not send setup tests except `os-release`
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

    def _nodes_processed(self, nodes):
        """
        Mark the node as processed, sent_kcidb field to True
        That means kcidb has received the node, and at least tried to process it
        Data in node might be invalid, and not really sent to kcidb
        This is workaround, until we improve event handling in kernelci-pipeline/api
        """
        self.log.info(f"Marking {len(nodes)} nodes flag as processed")
        self._api.node.bulkset(nodes, 'processed_by_kcidb_bridge', 'True')

    def _node_processed_recursively(self, node):
        """
        Mark the node and its child nodes as processed
        """
        nodeids = []
        child_nodes = self._api.node.find({'parent': node['id']})
        if child_nodes:
            for child in child_nodes:
                nodeids.append(child['id'])
        return nodeids

    def _find_unprocessed_node(self, chunksize):
        """
        Search for 96h nodes that were not sent to KCIDB
        This is nodes in available/completed state, and where flag
        sent_kcidb is not set
        If we don't have anymore unprocessed nodes, we will wait for 30 minutes
        before we search again.
        """
        if self._last_unprocessed_search and \
                time.time() - self._last_unprocessed_search < 30 * 60:
            return None
        nodes = self._api.node.findfast({
            'state': 'done',
            'processed_by_kcidb_bridge': False,
            'created__gt': datetime.datetime.now() - datetime.timedelta(days=4),
            'limit': chunksize
        })
        if nodes:
            return nodes
        else:
            self._last_unprocessed_search = time.time()
        return []

    def _submit_parsed_data(self, checkouts, builds, tests, issues, incidents, ctx_client):
        revision = {
            'checkouts': checkouts,
            'builds': builds,
            'tests': tests,
            'issues': issues,
            'incidents': incidents,
            'version': {
                'major': 4,
                'minor': 5
            }
        }
        self._send_revision(ctx_client, revision)
        if len(checkouts) > 0:
            for checkout in checkouts:
                self.log.info(f"Sent checkout node: {checkout['id']}")
        if len(builds) > 0:
            for build in builds:
                self.log.info(f"Sent build node: {build['id']}")
        if len(tests) > 0:
            for test in tests:
                self.log.info(f"Sent test node: {test['id']}")
        if len(issues) > 0:
            for issue in issues:
                self.log.info(f"Sent issue node: {issue['id']}")
        if len(incidents) > 0:
            for incident in incidents:
                self.log.info(f"Sent incident node: {incident['id']}")

    def _run(self, context):
        self.log.info("Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")
        chunksize = 200
        nodes = []
        batchcheckouts = []
        batchbuilds = []
        batchtests = []
        batchissues = []
        batchincidents = []
        batchnodes = []

        while True:
            is_hierarchy = False
            if not nodes or len(nodes) == 0:
                # if no batched nodes to process anymore, submit accumulated data
                if len(batchcheckouts) > 0 or len(batchbuilds) > 0 or len(batchtests) > 0 or\
                   len(batchissues) > 0 or len(batchincidents) > 0:
                    try:
                        self._submit_parsed_data(batchcheckouts, batchbuilds, batchtests,
                                                 batchissues, batchincidents, context['client'])
                        chunksize = 200
                    except Exception as exc:
                        self.log.error(f"Failed to submit data to KCIDB: {str(exc)}")
                        # do not mark nodes as processed, as they were not sent to KCIDB
                        batchnodes = []
                        # sometimes we get too much data and exceed gcloud limits,
                        # try to send smaller chunks
                        chunksize = 50

                if len(batchnodes) > 0:
                    self._nodes_processed(batchnodes)
                # and reset the lists
                batchcheckouts = []
                batchbuilds = []
                batchtests = []
                batchissues = []
                batchincidents = []
                batchnodes = []
                # clean caches
                self._nodecache = {}
                self._excerptcache = {}
                # If we have no more batched nodes to process, try to find new ones
                nodes = self._find_unprocessed_node(chunksize)
                self.log.info(f"Found {len(nodes)} unprocessed nodes")

            if not nodes or len(nodes) == 0:
                node, is_hierarchy = self._api_helper.receive_event_node(context['sub_id'])
                self.log.info(f"Received an event for node: {node['id']}")
            else:
                node = nodes.pop()
                self.log.info(f"Processing unprocessed node: {node['id']}")

            # Submit nodes with service origin only for staging pipeline
            if self._current_user['username'] in ('staging.kernelci.org',
                                                  'production'):
                if node['submitter'] != 'service:pipeline':
                    self.log.debug(f"Not sending node to KCIDB: {node['id']}")
                    batchnodes.append(node['id'])
                    continue

            parsed_checkout_node = []
            parsed_build_node = []
            parsed_test_node = []
            issues = []
            incidents = []

            if node['kind'] == 'checkout':
                parsed_checkout_node = self._parse_checkout_node(
                    context['origin'], node)
                batchcheckouts.extend(parsed_checkout_node)

            elif node['kind'] == 'kbuild':
                parsed_build_node = self._parse_build_node(
                    context['origin'], node
                )
                batchbuilds.extend(parsed_build_node)

            elif node['kind'] == 'test':
                self._get_test_data(node, context['origin'],
                                    parsed_test_node, parsed_build_node)
                batchtests.extend(parsed_test_node)
                batchbuilds.extend(parsed_build_node)

            elif node['kind'] == 'job':
                # Send job node
                self._get_test_data(node, context['origin'],
                                    parsed_test_node, parsed_build_node)
                if is_hierarchy:
                    self._get_test_data_recursively(node, context['origin'],
                                                    parsed_test_node, parsed_build_node)
                batchtests.extend(parsed_test_node)
                batchbuilds.extend(parsed_build_node)

            for parsed_node in parsed_build_node:
                if parsed_node.get('valid') is False and parsed_node.get('log_url'):
                    local_file = self._cached_fetch(parsed_node['log_url'])
                    local_url = f"file://{local_file}"
                    issues_and_incidents = generate_issues_and_incidents(
                        parsed_node['id'],
                        local_url,
                        "build",
                        context['kcidb_oo_client'])
                    if issues_and_incidents:
                        issues.extend(issues_and_incidents.get('issues', []))
                        incidents.extend(issues_and_incidents.get('incidents', []))
                        batchissues.extend(issues)
                        batchincidents.extend(incidents)

                        self.log.debug(f"Generated issues/incidents: {issues_and_incidents}")
                    else:
                        self.log.warning("logspec: Could not generate any issues or "
                                         f"incidents for build node {parsed_node['id']}")

            for parsed_node in parsed_test_node:
                if parsed_node.get('status') == 'FAIL' and parsed_node.get('log_url'):
                    local_file = self._cached_fetch(parsed_node['log_url'])
                    local_url = f"file://{local_file}"
                    issues_and_incidents = generate_issues_and_incidents(
                        parsed_node['id'],
                        local_url,
                        "test",
                        context['kcidb_oo_client'])
                    if issues_and_incidents:
                        issues.extend(issues_and_incidents.get('issues', []))
                        incidents.extend(issues_and_incidents.get('incidents', []))
                        batchissues.extend(issues)
                        batchincidents.extend(incidents)
                        self.log.debug(f"Generated issues/incidents: {issues_and_incidents}")
                    else:
                        self.log.warning("logspec: Could not generate any issues or "
                                         f"incidents for test node {parsed_node['id']}")

            # mark the node as processed, sent_kcidb field to True
            # TBD: job nodes might have child nodes, mark them as processed as well
            batchnodes.append(node['id'])
            if is_hierarchy:
                childnodes = self._node_processed_recursively(node)
                batchnodes.extend(childnodes)
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
            'name': '--name',
            'help': "Name of pipeline instance",
        },
        {
            'name': '--origin',
            'help': "CI system identifier",
        },
        {
            'name': '--database-name',
            'help': "KCIDB postgresql database instance name",
        },
        {
            'name': '--postgresql-host',
            'help': "KCIDB postgresql DB host",
        },
        {
            'name': '--postgresql-port',
            'help': "KCIDB postgresql DB port",
        },
        {
            'name': '--postgresql-user',
            'help': "Username for connecting to KCIDB postgresql DB",
        },
        {
            'name': '--postgresql-password',
            'help': "Password for connecting to KCIDB postgresql DB",
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
