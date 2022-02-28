#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import datetime
import json
import os
import sys

import kernelci
import kernelci.config
import kernelci.data
from kernelci.cli import Args, Command, parse_opts


class cmd_run(Command):
    help = "Listen for events and report them on stdout"
    args = [Args.db_config]

    def __call__(self, configs, args):
        log_fmt = "{time:26s}  {commit:12s}  {status:8s}  {name}"

        status_map = {
            "pending": "Pending",
            "pass": "Pass",
            "fail": "Fail",
        }

        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.data.get_db(db_config, api_token)

        sub_id = db.subscribe('node')
        print("Listening for events... ")
        print("Press Ctrl-C to stop.")
        sys.stdout.flush()

        try:
            print(log_fmt.format(
                time="Time", commit="Commit", status="Status", name="Name"
            ))
            while True:
                event = db.get_event(sub_id)
                dt = datetime.datetime.fromisoformat(event['time'])
                obj = db.get_node_from_event(event)
                print(log_fmt.format(
                    time=dt.strftime('%Y-%m-%d %H:%M:%S.%f'),
                    commit=obj['revision']['commit'][:12],
                    status=status_map[obj['status']],
                    name=obj['name'],
                ))
                sys.stdout.flush()
        except KeyboardInterrupt as e:
            print("Stopping.")
        finally:
            db.unsubscribe(sub_id)

        sys.stdout.flush()


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
