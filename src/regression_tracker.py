#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import logging
import os
import sys

import kernelci
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts

from logger import Logger


class RegressionTracker:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._logger = Logger("config/logger.conf", "regression_tracker")
        self._regression_fields = [
            'artifacts', 'group', 'name', 'path', 'revision', 'result',
            'state',
        ]

    def _create_regression(self, failed_node, last_successful_node):
        """Method to create a regression"""
        regression = {}
        for field in self._regression_fields:
            regression[field] = failed_node[field]
        regression['parent'] = failed_node['_id']
        regression['regression_data'] = [last_successful_node, failed_node]
        self._db.submit({'regression': regression})

    def run(self):
        """Method to run regression tracking"""
        sub_id = self._db.subscribe_node_channel(filters={
            'state': 'done',
            'result': 'fail',
        })
        self._logger.log_message(logging.INFO, "Tracking regressions... ")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")
        sys.stdout.flush()

        try:
            while True:
                node = self._db.receive_node(sub_id)
                if not node['group']:
                    continue

                previous_nodes = self._db.get_nodes({
                    'name': node['name'],
                    'group': node['group'],
                    'revision.tree': node['revision']['tree'],
                    'revision.branch': node['revision']['branch'],
                    'revision.url': node['revision']['url'],
                    'created__lt': node['created'],
                })

                if not previous_nodes:
                    continue

                previous_nodes = sorted(
                    previous_nodes,
                    key=lambda node: node['created'],
                    reverse=True
                )

                if previous_nodes[0]['result'] == 'pass':
                    self._logger.log_message(logging.INFO, f"Detected \
regression for node id: {node['_id']}")
                    self._create_regression(node, previous_nodes[0])

                sys.stdout.flush()
        except KeyboardInterrupt as err:
            self._logger.log_message(logging.INFO, "Stopping.")
        finally:
            self._db.unsubscribe(sub_id)

        sys.stdout.flush()


class cmd_run(Command):
    help = "Regression tracker"
    args = [Args.db_config]

    def __call__(self, configs, args):
        regression_tracker = RegressionTracker(configs, args)
        regression_tracker.run()


if __name__ == '__main__':
    opts = parse_opts('regression_tracker', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
