#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Maryam Yusuf
# Author: Maryam Yusuf <maryam.m.yusuf1802@gmail.com>

import os
import sys

import kernelci
import kernelci.data
from kernelci.config import load
from kernelci.cli import Args, Command, parse_opts


class cmd_run(Command):
    help = "Listen for events and send them to KCDIB"
    args = [Args.db_config]

    def __call__(self, configs, args):
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.data.get_db(db_config, api_token)

        sub_id = db.subscribe('node')
        print("Listening for events... ")
        print("Press Ctrl-C to stop.")
        sys.stdout.flush()

        try:
            while True:
                event = db.get_event(sub_id)
                node = db.get_node_from_event(event)
                if node['name'] != 'checkout':
                    continue
                print(f"Printing node for {node}")
                sys.stdout.flush()

            sys.stdout.flush()

        except KeyboardInterrupt:
            print("Stopping.")
        finally:
            db.unsubscribe(sub_id)

        sys.stdout.flush()


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
