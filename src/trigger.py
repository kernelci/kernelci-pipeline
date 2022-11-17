#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import json
import logging
import sys
import time

import kernelci
import kernelci.build
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts
import urllib
import requests

from base import Service


class Trigger(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'trigger')
        self._build_configs = configs['build_configs']

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

    def _setup(self, args):
        ctx = Service.Context()
        ctx.data.update({
            'poll_period': int(args.poll_period),
            'force': args.force,
        })
        return ctx

    def _run(self, ctx):
        poll_period, force = (
            ctx.data[key] for key in ('poll_period', 'force')
        )
        while True:
            self._iterate_build_configs(force)
            if poll_period:
                self._logger.log_message(
                    logging.INFO,
                    f"Sleeping for {poll_period}s"
                )
                time.sleep(poll_period)
            else:
                self._logger.log_message(logging.INFO, "Not polling.")
                break
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
        return Trigger(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
