#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import sys

import kernelci
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service


class GetBuilds(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'get_builds')

    def _setup(self, args):
        return self._api_helper.subscribe_filters({
            'kind': 'kbuild',
            'state': 'done',
            'result': 'pass'
        }, 'build')

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)
        sys.stdout.flush()

    def _run(self, sub_id):
        self.log.info("Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            node, _ = self._api_helper.receive_event_node(sub_id)
            self.log.info(f"Build node received: {node}")
            self.log.info(f"artifacts: {node.get('artifacts')}")
        return True


class cmd_run(Command):
    help = "Listen for successful kernel build events"
    args = [Args.api_config]

    def __call__(self, configs, args):
        return GetBuilds(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('get_builds', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
