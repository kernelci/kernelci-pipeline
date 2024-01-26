#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

from datetime import datetime, timedelta
import json
import logging
import sys
import time

import kernelci
import kernelci.build
import kernelci.config
import kernelci.db
from kernelci.legacy.cli import Args, Command, parse_opts
import urllib
import requests

from base import Service


class Trigger(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'trigger')
        self._build_configs = configs['build_configs']
        self._current_user = self._api.user.whoami()

    def _log_revision(self, message, build_config, head_commit):
        self.log.info(f"{message:32s} {build_config.name:32s} {head_commit}")

    def _run_trigger(self, build_config, force, timeout):
        head_commit = kernelci.build.get_branch_head(build_config)
        node_count = self._api.node.count({
            "kind": "checkout",
            "data.kernel_revision.commit": head_commit,
            "owner": self._current_user['username'],
        })

        if node_count > 0:
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
        checkout_timeout = datetime.utcnow() + timedelta(minutes=timeout)
        node = {
            'name': 'checkout',
            'path': ['checkout'],
            'kind': 'checkout',
            'data': {
                'kernel_revision': revision,
            },
            'timeout': checkout_timeout.isoformat(),
        }
        try:
            self._api.node.add(node)
        except requests.exceptions.HTTPError as ex:
            detail = ex.response.json().get('detail')
            if detail:
                self.log.error(detail)
        except Exception as ex:
            self.traceback(ex)

    def _setup(self, args):
        return {
            'poll_period': int(args.poll_period),
            'force': args.force,
            'build_configs_list': (args.build_configs or '').split(),
            'startup_delay': int(args.startup_delay or 0),
            'timeout': args.timeout,
        }

    def _run(self, ctx):
        if ctx['startup_delay']:
            self.log.info(f"Delay: {ctx['startup_delay']}s")
            time.sleep(ctx['startup_delay'])

        while True:
            # Iterate through build configs
            for name, config in self._build_configs.items():
                if (not ctx['build_configs_list'] or
                    name in ctx['build_configs_list']):  # noqa
                    self._run_trigger(config, ctx['force'], ctx['timeout'])
            if ctx['poll_period']:
                self.log.info(f"Sleeping for {ctx['poll_period']}s")
                time.sleep(ctx['poll_period'])
            else:
                self.log.info("Not polling.")
                break

        return True


class cmd_run(Command):
    help = "Submit a new revision to the API based on local git repo"
    args = [
        Args.api_config,
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
        {
            'name': '--build-configs',
            'help': "List of build configurations to monitor",
        },
        {
            'name': '--startup-delay',
            'type': int,
            'help': "Delay loop at startup by a number of seconds",
        },
        {
            'name': '--timeout',
            'type': float,
            'help': "Timeout minutes for checkout node",
        },
    ]

    def __call__(self, configs, args):
        return Trigger(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    yaml_configs = opts.get_yaml_configs() or 'config/pipeline.yaml'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
