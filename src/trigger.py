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
import hashlib

from base import Service, validate_url


class Trigger(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'trigger')
        self._build_configs = configs['build_configs']
        self._current_user = self._api.user.whoami()

    def _log_revision(self, message, build_config, head_commit):
        self.log.info(f"{message:32s} {build_config.name:32s} {head_commit}")

    def _run_trigger(self, build_config, force, timeout, trees):
        if trees and len(trees) > 1:
            tree_condition = "not" if trees.startswith("!") else "only"
            trees_list = trees.strip("!").split(",")  # Remove leading '!', split by comma
            tree_in_list = build_config.tree.name in trees_list
            if (tree_in_list and tree_condition == "not") or \
               (not tree_in_list and tree_condition == "only"):
                return

        try:
            if validate_url(build_config.branch):
                response = requests.get(build_config.branch)
                # Following extractor supports only NIPA JSON scheme.
                # Adding support for other schemas will force moving it to a separate function.
                branches = response.json()
                latest = sorted(branches, key=lambda x: x['date'], reverse=True)[0]
                tree = build_config.tree.name
                self.log.info(f"NIPA Latest branch: {latest['branch']} Date: {latest['date']}"
                              f" Tree: {tree}")
                build_config._branch = latest['branch']

            head_commit = kernelci.build.get_branch_head(build_config)
        except Exception as ex:
            self.log.error(f"Failed to get branch head for {build_config.name:32s}")
            self.traceback(ex)
            return
        search_terms = {
            "kind": "checkout",
            "data.kernel_revision.commit": head_commit,
            "owner": self._current_user['username'],
            "submitter": "service:pipeline"
        }
        node_count = self._api.node.count(search_terms)
        search_terms["result"] = "incomplete"
        incomplete_node_count = self._api.node.count(search_terms)

        # If incomplete_node_count >= 3, then we have a problem
        # it means we retry the same commit 3 times and it still fails
        if incomplete_node_count >= 3:
            self._log_revision(
                "Too many incomplete checkouts", build_config, head_commit
            )
            return

        # Do not count incomplete checkouts
        if node_count > 0 and incomplete_node_count != node_count:
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
        # treeid is sha256(url+branch+timestamp)
        hashstr = revision['url'] + revision['branch'] + str(datetime.now())
        treeid = hashlib.sha256(hashstr.encode()).hexdigest()
        node = {
            'name': 'checkout',
            'path': ['checkout'],
            'kind': 'checkout',
            'data': {
                'kernel_revision': revision,
            },
            'timeout': checkout_timeout.isoformat(),
            'treeid': treeid,
        }
        if self._current_user['username'] in ('staging.kernelci.org',
                                              'production'):
            node['submitter'] = 'service:pipeline'
        else:
            node['submitter'] = f"user:{self._current_user['email']}"

        try:
            self._api.node.add(node)
        except requests.exceptions.HTTPError as ex:
            detail = ex.response.json().get('detail')
            if detail:
                self.log.error(detail)
        except Exception as ex:
            self.traceback(ex)

    def _iterate_build_configs(self, force, build_configs_list,
                               timeout, trees):
        for name, config in self._build_configs.items():
            if not build_configs_list or name in build_configs_list:
                cfg_copy = config.copy()
                self._run_trigger(cfg_copy, force, timeout, trees)

    def _setup(self, args):
        return {
            'poll_period': int(args.poll_period),
            'force': args.force,
            'build_configs_list': (args.build_configs or '').split(),
            'startup_delay': int(args.startup_delay or 0),
            'timeout': args.timeout,
            'trees': args.trees,
        }

    def _run(self, ctx):
        poll_period, force, build_configs_list, startup_delay, timeout, trees = (
            ctx[key] for key in (
                'poll_period', 'force', 'build_configs_list', 'startup_delay',
                'timeout', 'trees'
            )
        )

        if startup_delay:
            self.log.info(f"Delay: {startup_delay}s")
            time.sleep(startup_delay)

        while True:
            self._iterate_build_configs(force, build_configs_list,
                                        timeout, trees)
            if poll_period:
                self.log.info(f"Sleeping for {poll_period}s")
                time.sleep(poll_period)
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
            'name': '--name',
            'help': "Name of pipeline instance",
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
        {
            'name': '--trees',
            'help': "Exclude or include certain trees (default: all), " +
                    "!kernelci for all except kernelci" +
                    "kernelci for only kernelci" +
                    "!kernelci,linux not kernelci and not linux",
        },
    ]

    def __call__(self, configs, args):
        return Trigger(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('trigger', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
