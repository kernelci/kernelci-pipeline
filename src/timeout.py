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

from logger import Logger


class Timeout:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._logger = Logger("config/logger.conf", "timeout")
        self._poll_period = args.poll_period

    def _update_available_node(self, node, current_time):
        """Set node state to 'done' if holdoff is expired and all child nodes
        are completed.
        Set node state to `closing` if holdoff is expired and at least one of
        the child nodes is incomplete"""
        holdoff_time = datetime.fromisoformat(node['holdoff'])
        if current_time > holdoff_time:
            total_child_nodes = self._db.count_nodes({
                'parent': node['_id'],
            })

            completed_child_nodes = self._db.count_nodes({
                'parent': node['_id'],
                'state': 'done'
            })

            if total_child_nodes > completed_child_nodes:
                node['state'] = 'closing'
            else:
                node['state'] = 'done'
            self._db.submit({'node': node})

    def _update_child_nodes(self, parent_id):
        """Set child node state to done when parent is timed out"""
        child_nodes = self._db.get_nodes({'parent': parent_id})
        for child in child_nodes:
            if child['state'] != 'done':
                child['state'] = 'done'
                self._db.submit({'node': child})
                self._update_child_nodes(child['_id'])

    def _update_timed_out_node(self, node):
        """Set Node state to done if maximum wait time is over"""
        if node['state'] != 'done':
            current_time = datetime.utcnow()
            expires = datetime.fromisoformat(node['created']) + \
                timedelta(hours=node['timeout'])
            if current_time > expires:
                node['state'] = 'done'
                self._db.submit({'node': node})
                self._update_child_nodes(node['_id'])
            elif node['state'] == 'available':
                self._update_available_node(node, current_time)

    def run(self):
        self._logger.log_message(logging.INFO,
                                 "Checking timed-out nodes...")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                nodes = self._db.get_nodes()
                for node in nodes:
                    self._update_timed_out_node(node)
                sleep(self._poll_period)
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())


class cmd_run(Command):
    help = "Set node state to done if maximum wait time is over"
    args = [
        Args.db_config,
        {
            'name': '--poll-period',
            'type': int,
            'help': "Polling period in seconds",
            'default': 25,
        },
    ]

    def __call__(self, configs, args):
        timeout = Timeout(configs, args)
        timeout.run()


if __name__ == '__main__':
    opts = parse_opts('timeout', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
