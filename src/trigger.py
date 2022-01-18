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

DEFAULT_URL = 'http://172.17.0.1:8001'


def get_node_by_commit_id(commit_id):
    """
    Get node object by sending commit ID to 'nodes' endpoint
    """
    get_node_path = '?'.join(['nodes', 'revision.commit='+commit_id])
    url = urllib.parse.urljoin(DEFAULT_URL, get_node_path)
    return requests.get(url).json()


def _run_trigger(args, build_config, db):
    if args.skip_pull:
        print("Not updating repo")
    else:
        head_commit = kernelci.build.get_branch_head(build_config)
        node_list = get_node_by_commit_id(head_commit)
        if node_list:
            print(f"Node exists with the latest git commit: {head_commit}")
            return
        print(f"Updating repo: {args.kdir}")
        sys.stdout.flush()
        kernelci.build.update_repo(build_config, args.kdir)

    print("Gathering revision meta-data")
    sys.stdout.flush()
    step = kernelci.build.RevisionData(args.kdir, args.output, reset=True)
    res = step.run(opts={
        'tree': build_config.tree.name,
        'url': build_config.tree.url,
        'branch': build_config.branch,
    })
    meta = kernelci.build.Metadata(args.output)
    revision = meta.get('bmeta', 'revision')

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
        Args.kdir, Args.build_config, Args.output, Args.db_config,
    ]
    opt_args = [
        {
            'name': '--skip-pull',
            'action': 'store_true',
            'help': "Skip git pull",
        },
        {
            'name': '--poll-period',
            'type': int,
            'help': "Polling period in seconds, disabled by default",
            'default': 0,
        },
    ]

    def __call__(self, configs, args):
        build_config = configs['build_configs'][args.build_config]
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.data.get_db(db_config, api_token)

        while True:
            _run_trigger(args, build_config, db)
            if args.poll_period:
                time.sleep(args.poll_period)
            else:
                break

        return True


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
