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


class SetTimeout:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._logger = Logger("config/logger.conf", "set_timeout")
        self._poll_period = args.poll_period

    def _update_pending_child(self, parent_id):
        """Set child node status to timeout when parent is timed out"""
        child_nodes = self._db.get_nodes({"status": "pending",
                                          "parent": parent_id})
        for child in child_nodes:
            child['status'] = "timeout"
            self._db.submit({'node': child})
            self._update_pending_child(child["_id"])

    def _set_timeout_status(self, node):
        """Set Node status to timeout if maximum wait time is over"""
        current_time = datetime.utcnow()
        max_wait_time = datetime.fromisoformat(node['created']) + \
            timedelta(hours=node['max_wait_time'])
        if current_time > max_wait_time:
            node['status'] = "timeout"
            self._db.submit({'node': node})
            self._update_pending_child(node["_id"])

    def run(self):
        self._logger.log_message(logging.INFO,
                                 "Checking pending nodes...")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                nodes = self._db.get_nodes({"status": "pending"})
                for node in nodes:
                    self._set_timeout_status(node)
                sleep(self._poll_period)
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())


class cmd_run(Command):
    help = "Set node status to timeout if maximum wait time is over"
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
        set_timeout = SetTimeout(configs, args)
        set_timeout.run()


if __name__ == '__main__':
    opts = parse_opts('set_timeout', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
