#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

from datetime import datetime, timedelta
import logging
import os
import sys
import time
import traceback

import kernelci
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts

from logger import Logger


class CompleteHack:

    def __init__(self, configs, args):
        self._logger = Logger("config/logger.conf", "complete")
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._pending_node = None

    def _check_pending_node(self):
        now = datetime.utcnow()
        created = datetime.fromisoformat(self._pending_node['created'])
        expires = created + timedelta(minutes=10)
        if now > expires:
            self._pending_node['state'] = "done"
            self._db.submit({'node': self._pending_node})
            self._pending_node = None

    def run(self):
        sub_id = self._db.subscribe_node_channel({
            'name': 'checkout',
            'state': 'available',
        })
        self._logger.log_message(logging.INFO, "Listening for new checkouts")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                if not self._pending_node:
                    self._pending_node = self._db.receive_node(sub_id)
                else:
                    time.sleep(5)
                    self._check_pending_node()
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())
        finally:
            self._db.unsubscribe(sub_id)
            return True


class cmd_run(Command):
    help = "Hack to set checkout nodes to done state"
    args = [Args.db_config]
    opt_args = [Args.verbose]

    def __call__(self, configs, args):
        CompleteHack(configs, args).run()
        return True


if __name__ == '__main__':
    opts = parse_opts('complete_hack', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
