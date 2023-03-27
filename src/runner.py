#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import logging
import os
import sys
import yaml

import kernelci
import kernelci.config
import kernelci.runtime
import kernelci.storage
from kernelci.cli import Args, Command, parse_opts

from base import Service
from job import Job


class Runner(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'runner')
        self._api_config_yaml = yaml.dump(self._api_config)
        self._plan_configs = configs['test_plans']
        self._device_configs = configs['device_types']
        self._verbose = args.verbose
        self._storage_config = configs['storage_configs'][args.storage_config]
        storage_cred = os.getenv('KCI_STORAGE_CREDENTIALS')
        self._storage = kernelci.storage.get_storage(
            self._storage_config, storage_cred
        )
        self._job = Job(
            self._api_helper,
            self._api_config_yaml,
            configs['runtimes'][args.runtime_config],
            self._storage,
            args.output
        )


class RunnerLoop(Runner):
    """Runner subclass to execute in a loop"""

    def __init__(self, configs, args, **kwargs):
        super().__init__(configs, args, **kwargs)
        self._job_tmp_dirs = {}
        self._plans = [self._plan_configs[plan] for plan in args.plans]

    def _cleanup_paths(self):
        job_tmp_dirs = {
            process: tmp
            for process, tmp in self._job_tmp_dirs.items()
            if process.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?

    def _setup(self, args):
        return self._api_helper.subscribe_filters({
            'name': 'checkout',
            'state': 'available',
        })

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)
        self._cleanup_paths()

    def _run(self, sub_id):
        self.log.info("Listening for available checkout events")
        self.log.info("Press Ctrl-C to stop.")

        # ToDo: iterate over device types for the current runtime
        device_type = self._job.get_device_type()
        device = self._device_configs.get(device_type)
        if device is None:
            self.log.error(f"Device type not found: {device_type}")
            return False

        while True:
            checkout_node = self._api_helper.receive_event_node(sub_id)
            for plan in self._plans:
                node, msg = self._job.create_node(checkout_node, plan)
                if not node:
                    self.log.error(
                        f"Failed to create node for {plan.name}: {msg}"
                    )
                    continue
                job, tmp = self._job.schedule_job(node, plan, device)
                if not job:
                    self.log.error(
                        f"Failed to schedule job for {plan.name}: {tmp}"
                    )
                    continue
                self.log.info(' '.join([
                    node['_id'],
                    self._job.runtime_name,
                    str(self._job.get_id(job)),
                ]))
                if device_type == 'shell':
                    self._job_tmp_dirs[job] = tmp

        return True


class RunnerSingleJob(Runner):
    """Runner subclass to execute a single job"""

    def _get_node_from_commit(self, git_commit):
        nodes = self._api.get_nodes({
            "revision.commit": git_commit,
        })
        return nodes[0] if nodes else None

    def _setup(self, args):
        if args.node_id:
            checkout_node = self._api.get_node(args.node_id)
        elif args.git_commit:
            checkout_node = self._get_node_from_commit(args.git_commit)
        else:
            checkout_node = None
        if checkout_node is None:
            self._logger.log_message(logging.ERROR, "Node not found")
            return False
        return {
            'node': checkout_node,
            'plan': self._plan_configs[args.plan],
            'device': self._device_configs[args.target],
        }

    def _run(self, ctx):
        node, plan, device = (ctx[key] for key in ('node', 'plan', 'device'))
        job, tmp = self._job.schedule_job(node, plan, device)
        if not job:
            self.log.error(
                f"Failed to schedule job for {plan.name}. Error: {tmp}"
            )
            return False
        if self._job.get_device_type() == 'shell':
            self.log.info("Waiting...")
            job.wait()
            self.log.info("...done")
        return True


class cmd_loop(Command):
    help = "Listen to pub/sub events and run in a loop"
    args = [Args.api_config, Args.runtime_config, Args.output]
    opt_args = [
        Args.verbose,
        {
            'name': 'plans',
            'nargs': '+',
            'help': "Test plans to run",
        },
    ]

    def __call__(self, configs, args):
        return RunnerLoop(configs, args).run()


class cmd_run(Command):
    help = "Run one arbitrary test and exit"
    args = [
        Args.api_config, Args.runtime_config, Args.storage_config,
        Args.output, Args.plan, Args.target,
    ]
    opt_args = [
        Args.verbose,
        {
            'name': '--node-id',
            'help': "id of the checkout node rather than pub/sub",
        },
        {
            'name': '--git-commit',
            'help': "git commit rather than pub/sub event",
        },
    ]

    def __call__(self, configs, args):
        if not args.node_id and not args.git_commit:
            print("Either --node-id or --git-commit is required",
                  file=sys.stderr)
        try:
            return RunnerSingleJob(configs, args).run(args)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return False


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
