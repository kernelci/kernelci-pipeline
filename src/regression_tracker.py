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

    def _create_regression(self, node):
        """Method to create a regression"""
        regression = {
            'name': node['name'],
            'revision': node['revision'],
            'parent': node['_id'],
            'status': node['status'],
            'result': node['result'],
            'regression_data': [node],
            'artifacts': node['artifacts'],
        }
        self._db.submit({'regression': regression})

    def _add_regression_data(self, regression, node):
        """Method to record regression data"""
        regression['name'] = node['name']
        regression['revision'] = node['revision']
        regression['parent'] = node['_id']
        regression['status'] = node['status']
        regression['result'] = node['result']
        regression['artifacts'] = node['artifacts']
        regression['created'] = regression['created']
        regression['regression_data'].append(node)
        self._db.submit({'regression': regression})

    def run(self):
        """Method to run regression tracking"""
        sub_id = self._db.subscribe_node_channel(filters={
            'status': 'complete',
        })
        self._logger.log_message(logging.INFO, "Tracking regressions... ")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")
        sys.stdout.flush()

        try:
            while True:
                node = self._db.receive_node(sub_id)
                resp = self._db.get_regressions_by_node_id(node['_id'])
                if not resp and node['result'] == "pass":
                    self._create_regression(node)
                    self._logger.log_message(logging.INFO, f"Created \
regression for node id: {node['_id']}")
                elif resp and node['result'] == "fail":
                    self._add_regression_data(resp[0], node)
                    self._logger.log_message(logging.INFO, f"Added new \
regression data for node id: {node['_id']}")
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
