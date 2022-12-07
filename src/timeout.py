#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import logging
import os
import sys
from datetime import datetime, timedelta
from time import sleep
import traceback
import json
import requests

import kernelci
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts

from base import Service


class TimeoutService(Service):

    PENDING_STATES = ['running', 'available', 'closing']

    def __init__(self, configs, args, name):
        super().__init__(configs, args, name)
        self._poll_period = args.poll_period

    def _get_pending_nodes(self, filters=None):
        nodes = {}
        node_filters = filters.copy() if filters else {}
        for state in self.PENDING_STATES:
            node_filters['state'] = state
            for node in self._db.get_nodes(node_filters):
                nodes[node['_id']] = node
        return nodes

    def _count_running_child_nodes(self, parent_id):
        nodes_count = 0
        for state in self.PENDING_STATES:
            nodes_count += self._db.count_nodes({
                'parent': parent_id, 'state': state
            })
        return nodes_count

    def _get_child_nodes_recursive(self, node, state_filter=None):
        recursive = {}
        child_nodes = self._get_pending_nodes({'parent': node['_id']})
        for child_id, child in child_nodes.items():
            if state_filter is None or child['state'] == state_filter:
                recursive.update(self._get_child_nodes_recursive(
                    child, state_filter
                ))
        return recursive

    def _submit_lapsed_nodes(self, lapsed_nodes, state, log=None):
        for node_id, node in lapsed_nodes.items():
            node_update = node.copy()
            node_update['state'] = state
            if log:
                self.log.debug(f"{node_id} {log}")
            try:
                self._db.submit({'node': node_update})
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)


class Timeout(TimeoutService):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout')

    def _check_pending_nodes(self, pending_nodes):
        now = datetime.utcnow()
        sleep = timedelta(seconds=self._poll_period)
        timeout_nodes = {}
        for node_id, node in pending_nodes.items():
            timeout = datetime.fromisoformat(node['timeout'])
            if now > timeout:
                timeout_nodes[node_id] = node
                timeout_nodes.update(self._get_child_nodes_recursive(node))
            else:
                self.log.debug(f"{node_id} timeout left: {timeout - now}")
                sleep = min(sleep, timeout - now)
        self._submit_lapsed_nodes(timeout_nodes, 'done', 'TIMEOUT')
        return sleep.total_seconds()

    def _run(self, ctx):
        self.log.info("Looking for nodes with lapsed timeout...")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            pending_nodes = self._get_pending_nodes()
            sleep_time = self._check_pending_nodes(pending_nodes)
            sleep(sleep_time)

        return True


class Holdoff(TimeoutService):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout-holdoff')

    def _get_available_nodes(self):
        nodes = self._db.get_nodes({'state': 'available'})
        return {node['_id']: node for node in nodes}

    def _check_available_nodes(self, available_nodes):
        now = datetime.utcnow()
        sleep = timedelta(seconds=self._poll_period)
        timeout_nodes = {}
        closing_nodes = {}
        for node_id, node in available_nodes.items():
            holdoff = datetime.fromisoformat(node['holdoff'])
            if now > holdoff:
                running = self._count_running_child_nodes(node_id)
                if running:
                    closing_nodes.update(
                        self._get_child_nodes_recursive(node, 'available')
                    )
                    closing_nodes[node_id] = node
                else:
                    timeout_nodes.update(
                        self._get_child_nodes_recursive(node)
                    )
                    timeout_nodes[node_id] = node
            else:
                self.log.debug(f"{node_id} holdoff left: {holdoff - now}")
                sleep = min(sleep, holdoff - now)
        self._submit_lapsed_nodes(closing_nodes, 'closing', 'HOLDOFF')
        self._submit_lapsed_nodes(timeout_nodes, 'done', 'DONE')
        return sleep.total_seconds()

    def _run(self, ctx):
        self.log.info("Looking for nodes with lapsed holdoff...")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            available_nodes = self._get_available_nodes()
            sleep_time = self._check_available_nodes(available_nodes)
            sleep(sleep_time)

        return True


class Closing(TimeoutService):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout-closing')

    def _get_closing_nodes(self):
        nodes = self._db.get_nodes({'state': 'closing'})
        return {node['_id']: node for node in nodes}

    def _check_closing_nodes(self, closing_nodes):
        done_nodes = {}
        for node_id, node in closing_nodes.items():
            running = self._count_running_child_nodes(node_id)
            self.log.debug(f"{node_id} RUNNING: {running}")
            if not running:
                done_nodes[node_id] = node
        self._submit_lapsed_nodes(done_nodes, 'done', 'DONE')

    def _run(self, ctx):
        self.log.info("Looking for nodes that are done closing...")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            closing_nodes = self._get_closing_nodes()
            self._check_closing_nodes(closing_nodes)
            sleep(self._poll_period)

        return True


MODES = {
    'timeout': Timeout,
    'holdoff': Holdoff,
    'closing': Closing,
}


class cmd_run(Command):
    help = "Set node state to done if maximum wait time is over"
    args = [
        Args.db_config,
        {
            'name': '--poll-period',
            'type': int,
            'help': "Polling period in seconds",
            'default': 60,
        },
        {
            'name': '--mode',
            'choices': MODES.keys(),
        },
    ]

    def __call__(self, configs, args):
        return MODES[args.mode](configs, args).run()


if __name__ == '__main__':
    opts = parse_opts('timeout', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
