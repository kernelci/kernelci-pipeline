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

    def set_timeout_status(self, node):
        """Set Node status to timeout if maximum wait time is over"""
        current_time = datetime.utcnow()
        max_wait_time = datetime.strptime(node['created'],
                                          '%Y-%m-%dT%H:%M:%S.%f') + \
            timedelta(hours=node['max_wait_time'])
        if current_time > max_wait_time:
            node['status'] = "timeout"
            self._db.submit({'node': node})[0]
            return True
        return False

    def run(self):
        self._logger.log_message(logging.INFO,
                                 "Setting node status to timeout...")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                nodes = self._db.get_nodes({"status": "pending"})
                for node in nodes:
                    if node['parent'] is None:
                        # Do not set timeout for parent node
                        continue
                    if self.set_timeout_status(node):
                        resp = self._db.submit({'node': node})[0]
                        self._logger.log_message(logging.INFO, f"Set status \
to timeout of node: {resp['_id']}")
                sleep(1800)  # sleep for 30 min before next execution
        except KeyboardInterrupt as err:
            self._logger.log_message(logging.INFO, "Stopping.")


class cmd_run(Command):
    help = "Set node status to timeout if maximum wait time is over"
    args = [Args.db_config]

    def __call__(self, configs, args):
        set_timeout = SetTimeout(configs, args)
        set_timeout.run()


if __name__ == '__main__':
    opts = parse_opts('set_timeout', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
