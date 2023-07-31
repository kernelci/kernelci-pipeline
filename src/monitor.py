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
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service


class Monitor(Service):
    LOG_FMT = ("{time:26s}  {kind:15s} {commit:12s}  {id:24s} "
               "{state:9s}  {result:8s}  {name}")

    def __init__(self, configs, args):
        super().__init__(configs, args, 'monitor')
        self._log_titles = self.LOG_FMT.format(
            time="Time", kind="Kind", commit="Commit", id="Node Id",
            state="State", result="Result", name="Name")

    def _setup(self, args):
        return self._api.subscribe('node')

    def _stop(self, sub_id):
        if sub_id:
            self._api.unsubscribe(sub_id)
        sys.stdout.flush()

    def _run(self, sub_id):
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

        self.log.info("Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")
        print(self._log_titles, flush=True)

        while True:
            event = self._api.receive_event(sub_id)
            obj = event.data
            dt = datetime.datetime.fromisoformat(event['time'])
            try:
                commit = obj['data']['kernel_revision']['commit'][:12]
            except (KeyError, TypeError):
                commit = str(None)
            result = result_map[obj['result']] if obj['result'] else str(None)
            print(self.LOG_FMT.format(
                time=dt.strftime('%Y-%m-%d %H:%M:%S.%f'),
                kind=obj['kind'],
                commit=commit,
                id=obj['id'],
                state=state_map[obj['state']],
                result=result,
                name=obj['name']
            ), flush=True)

        return True


class cmd_run(Command):
    help = "Listen for events and report them on stdout"
    args = [Args.api_config]

    def __call__(self, configs, args):
        return Monitor(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('monitor', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
