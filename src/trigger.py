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


def _run_trigger(args, build_config, db, logger):
    head_commit = kernelci.build.get_branch_head(build_config)
    node_list = db.get_nodes({
        "revision.commit": head_commit,
    })
    if node_list:
        logger.log_message(logging.INFO, f"Node exists with \
the latest git commit {head_commit}")
        if args.force:
            logger.log_message(logging.INFO, "Creating new checkout node \
anyway")
        else:
            return
    sys.stdout.flush()

    revision = {
        'tree': build_config.tree.name,
        'url': build_config.tree.url,
        'branch': build_config.branch,
        'commit': head_commit
    }

    logger.log_message(logging.INFO, f"Sending revision node to API: \
{revision['commit']}")
    sys.stdout.flush()
    node = {
        'name': 'checkout',
        'revision': revision,
        'state': 'available',
    }
    resp_obj = db.submit({'node': node})[0]
    node_id = resp_obj['_id']
    logger.log_message(logging.INFO, f"Node id: {node_id}")
    sys.stdout.flush()


class cmd_run(Command):
    help = "Submit a new revision to the API based on local git repo"
    args = [
        Args.build_config, Args.db_config,
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

    def __init__(self, sub_parser, name):
        super().__init__(sub_parser, name)
        self._logger = Logger("config/logger.conf", "trigger")

    def __call__(self, configs, args):
        build_config = configs['build_configs'][args.build_config]
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.db.get_db(db_config, api_token)
        poll_period = int(args.poll_period)

        while True:
            try:
                _run_trigger(args, build_config, db, self._logger)
                if poll_period:
                    time.sleep(poll_period)
                else:
                    break
            except KeyboardInterrupt:
                self._logger.log_message(logging.INFO, "Stopping.")
            except Exception:
                self._logger.log_message(logging.ERROR, traceback.format_exc())

        return True


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
