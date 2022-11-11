#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import json
import logging
import os
import sys
import time
import traceback

import kernelci
import kernelci.build
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts
import urllib
import requests

from logger import Logger


class Trigger():

    def __init__(self, configs, args):
        self._logger = Logger("config/logger.conf", "trigger")
        self._build_configs = configs['build_configs']
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(db_config, api_token)
        self._poll_period = int(args.poll_period)
        self._force = args.force

    def _log_revision(self, message, build_config, head_commit):
        self._logger.log_message(
            logging.INFO,
            f"{message:32s} {build_config.name:32s} {head_commit}"
        )

    def _run_trigger(self, build_config, force):
        head_commit = kernelci.build.get_branch_head(build_config)
        node_list = self._db.count_nodes({
            "revision.commit": head_commit,
        })

        if node_list:
            if force:
                self._log_revision(
                    "Resubmitting existing revision", build_config, head_commit
                )
            else:
                self._log_revision(
                    "Existing revision", build_config, head_commit
                )
                return
        else:
            self._log_revision(
                "New revision", build_config, head_commit
            )

        revision = {
            'tree': build_config.tree.name,
            'url': build_config.tree.url,
            'branch': build_config.branch,
            'commit': head_commit,
        }
        node = {
            'name': 'checkout',
            'path': ['checkout'],
            'revision': revision,
        }
        self._db.submit({'node': node})

    def _iterate_build_configs(self, force):
        for name, config in self._build_configs.items():
            self._run_trigger(config, force)

    def run(self):
        try:
            while True:
                self._iterate_build_configs(self._force)
                if self._poll_period:
                    self._logger.log_message(
                        logging.INFO,
                        f"Sleeping for {self._poll_period}s"
                    )
                    time.sleep(self._poll_period)
                else:
                    self._logger.log_message(logging.INFO, "Not polling.")
                    break
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())
            return False

        return True


class cmd_run(Command):
    help = "Submit a new revision to the API based on local git repo"
    args = [
        Args.db_config,
    ]
    opt_args = [
        {
            'name': '--poll-period',
            'type': int,
            'help': "Polling period in seconds, disabled when set to 0",
        },
        {
            'name': '--force',
            'action': 'store_true',
            'help': "Always create a new checkout node",
        },
    ]

    def __call__(self, configs, args):
        return Trigger(configs, args).run()


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
