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

import kernelci
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts

from base import Service


class TimeoutService(Service):

    def __init__(self, configs, args, name):
        super().__init__(configs, args, name)
        self._poll_period = args.poll_period

    def _get_pending_nodes(self, filters=None):
        nodes = {}
        node_filters = filters.copy() if filters else {}
        for state in ['running', 'available', 'closing']:
            node_filters['state'] = state
            for node in self._db.get_nodes(node_filters):
                nodes[node['_id']] = node
        return nodes

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
            self._db.submit({'node': node_update})


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


MODES = {
    'timeout': Timeout,
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
