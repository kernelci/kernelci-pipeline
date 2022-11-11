#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import datetime
import json
import logging
import sys

import kernelci
import kernelci.config
from kernelci.cli import Args, Command, parse_opts

from base import Service


class Notifier(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'notifier')

    def _setup(self, args):
        return self._db.subscribe('node')

    def _stop(self, sub_id):
        if sub_id:
            self._db.unsubscribe(sub_id)
        sys.stdout.flush()

    def _run(self, sub_id):
        log_fmt = \
            "{time:26s}  {commit:12s}  {id:24}  " \
            "{state:9s}  {result:8s}  {name}"

        state_map = {
            "running": "Running",
            "available": "Available",
            "closing": "Closing",
            "done": "Done",
        }

        result_map = {
            "pass": "Pass",
            "fail": "Fail",
            "skip": "Skipped",
            "incomplete": "Incomplete",
            None: "-",
        }

        self._logger.log_message(logging.INFO, "Listening for events... ")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")
        self._logger.log_message(logging.INFO, log_fmt.format(
            time="Time", commit="Commit", id="Node Id", state="State",
            result="Result", name="Name"
        ))
        sys.stdout.flush()

        while True:
            event = self._db.get_event(sub_id)
            dt = datetime.datetime.fromisoformat(event['time'])
            obj = self._db.get_node_from_event(event)
            self._logger.log_message(logging.INFO, log_fmt.format(
                time=dt.strftime('%Y-%m-%d %H:%M:%S.%f'),
                commit=obj['revision']['commit'][:12],
                id=obj['_id'],
                state=state_map[obj['state']],
                result=result_map[obj['result']],
                name=obj['name'],
            ))
            sys.stdout.flush()

        return True


class cmd_run(Command):
    help = "Listen for events and report them on stdout"
    args = [Args.db_config]

    def __call__(self, configs, args):
        return Notifier(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
