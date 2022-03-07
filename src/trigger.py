#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import json
import os
import sys
import time

import kernelci
import kernelci.build
import kernelci.config
import kernelci.data
from kernelci.cli import Args, Command, parse_opts
import urllib
import requests


def _run_trigger(args, build_config, db):
    head_commit = kernelci.build.get_branch_head(build_config)
    node_list = db.get_nodes_by_commit_hash(head_commit)
    if node_list:
        print(f"Node exists with the latest git commit {head_commit}")
        if args.force:
            print("Creating new checkout node anyway")
        else:
            return
    sys.stdout.flush()

    revision = {
        'tree': build_config.tree.name,
        'url': build_config.tree.url,
        'branch': build_config.branch,
        'commit': head_commit
    }

    print(f"Sending revision node to API: {revision['commit']}")
    sys.stdout.flush()
    node = {
        'name': 'checkout',
        'revision': revision,
    }
    resp_obj = db.submit({'node': node})[0]
    node_id = resp_obj['_id']
    print(f"Node id: {node_id}")
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

    def __call__(self, configs, args):
        build_config = configs['build_configs'][args.build_config]
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.data.get_db(db_config, api_token)
        poll_period = int(args.poll_period)

        while True:
            _run_trigger(args, build_config, db)
            if poll_period:
                time.sleep(poll_period)
            else:
                break

        return True


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
