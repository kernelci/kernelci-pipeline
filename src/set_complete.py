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


class SetComplete:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._logger = Logger("config/logger.conf", "set_complete")

    def run(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'status': ('complete', 'timeout'),
        })
        self._logger.log_message(logging.INFO, "Listening for node events... ")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                node = self._db.receive_node(sub_id)
                if node['parent'] is None:
                    continue
                nodes = self._db.get_child_nodes_from_parent(node['parent'])
                ret = False
                for node in nodes:
                    if node['status'] == "pending":
                        ret = True
                        break
                if ret:
                    continue
                root_node = self._db.get_root_node(node['_id'])
                root_node['status'] = "complete"
                resp = self._db.submit({'node': root_node})[0]
                self._logger.log_message(logging.INFO, f"Set status to \
complete of node: {resp['_id']}")
        except KeyboardInterrupt as e:
            self._logger.log_message(logging.INFO, "Stopping.")
        finally:
            self._db.unsubscribe(sub_id)


class cmd_run(Command):
    help = "Set root node status to 'complete' when all child nodes are \
completed"
    args = [Args.db_config]

    def __call__(self, configs, args):
        set_complete = SetComplete(configs, args)
        set_complete.run()


if __name__ == '__main__':
    opts = parse_opts('set_complete', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
