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


class Timeout(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout')
        self._poll_period = args.poll_period

    def _get_pending_nodes(self, filters=None):
        nodes = {}
        node_filters = filters.copy() if filters else {}
        for state in ['running', 'available', 'closing']:
            node_filters['state'] = state
            for node in self._db.get_nodes(node_filters):
                nodes[node['_id']] = node
        return nodes

    def _node_timeout(self, pending_nodes, node):
        node_id = node['_id']
        child_nodes = self._get_pending_nodes({'parent': node_id})
        for child_id, child in child_nodes.items():
            if child_id not in pending_nodes:
                self._node_timeout(pending_nodes, child)
        self.log.info(f"TIMEOUT {node_id}")
        done_node = node.copy()
        done_node['state'] = 'done'
        self._db.submit({'node': done_node})

    def _check_nodes(self, pending_nodes):
        now = datetime.utcnow()
        sleep = timedelta(seconds=self._poll_period)
        for node_id, node in pending_nodes.items():
            timeout = datetime.fromisoformat(node['timeout'])
            if now > timeout:
                self._node_timeout(pending_nodes, node)
            else:
                self.log.debug(f"{node_id} left: {timeout - now}")
                sleep = min(sleep, timeout - now)
        return sleep.total_seconds()

    def _run(self, ctx):
        self.log.info("Looking for timed-out nodes...")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            pending_nodes = self._get_pending_nodes()
            sleep_time = self._check_nodes(pending_nodes)
            sleep(sleep_time)

        return True


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
    ]

    def __call__(self, configs, args):
        return Timeout(configs, args).run()


if __name__ == '__main__':
    opts = parse_opts('timeout', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
