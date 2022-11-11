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
import tempfile

import kernelci
import kernelci.config
import kernelci.lab
from kernelci.cli import Args, Command, parse_opts

from base import Service


class Runner(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'runner')
        self._db_config_yaml = self._db_config.to_yaml()
        self._plan_configs = configs['test_plans']
        self._device_configs = configs['device_types']
        runtime_config = configs['labs'][args.lab_config]
        self._runtime = kernelci.lab.get_api(runtime_config)
        self._output = args.output
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        self._verbose = args.verbose

    def _create_node(self, checkout_node, plan_config):
        node = {
            'parent': checkout_node['_id'],
            'name': plan_config.name,
            'path': checkout_node['path'] + [plan_config.name],
            'group': plan_config.name,
            'artifacts': checkout_node['artifacts'],
            'revision': checkout_node['revision'],
        }
        return self._db.submit({'node': node})[0]

    def _generate_job(self, node, plan_config, device_config, tmp):
        self.log.info("Generating job")
        self.log.info(f"tmp: {tmp}")
        revision = node['revision']
        params = {
            'db_config_yaml': self._db_config_yaml,
            'name': plan_config.name,
            'node_id': node['_id'],
            'revision': revision,
            'runtime': self._runtime.config.lab_type,
            'runtime_image': plan_config.image,
            'tarball_url': node['artifacts']['tarball'],
            'workspace': tmp,
        }
        params.update(plan_config.params)
        params.update(device_config.params)
        templates = ['config/runtime', '/etc/kernelci/runtime']
        job = self._runtime.generate(
            params, device_config, plan_config, templates_paths=templates
        )
        output_file = self._runtime.save_file(job, tmp, params)
        self.log.info(f"output_file: {output_file}")
        return output_file

    def _schedule_test(self, checkout_node, plan, device):
        self.log.info("Tarball: {}".format(
            checkout_node['artifacts']['tarball']
        ))

        self.log.info("Creating test node")
        node = self._create_node(checkout_node, plan)

        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = self._generate_job(node, plan, device, tmp.name)

        self.log.info("Running test")
        job = self._runtime.submit(output_file)
        return job, tmp


class RunnerLoop(Runner):
    """Runner subclass to execute in a loop"""

    def __init__(self, configs, args, **kwargs):
        super().__init__(configs, args, **kwargs)
        self._job_tmp_dirs = {}
        self._plan = self._plan_configs[args.plan]

    def _cleanup_paths(self):
        job_tmp_dirs = {
            process: tmp
            for process, tmp in self._job_tmp_dirs.items()
            if process.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?

    def _setup(self, args):
        return self._db.subscribe_node_channel(filters={
            'name': 'checkout',
            'state': 'available',
        })

    def _stop(self, sub_id):
        if sub_id:
            self._db.unsubscribe(sub_id)
        self._cleanup_paths()

    def _run(self, sub_id):
        self.log.info("Listening for complete checkout events")
        self.log.info("Press Ctrl-C to stop.")

        # ToDo: iterate over device types for the current runtime
        device_type = self._runtime.config.lab_type
        device = self._device_configs.get(device_type)
        if device is None:
            self.log.error("Device type not found: {device_type}")
            return False

        while True:
            checkout_node = self._db.receive_node(sub_id)
            job, tmp = self._schedule_test(
                checkout_node, self._plan, device
            )
            if self._runtime.config.lab_type == 'shell':
                self._job_tmp_dirs[job] = tmp
            self._cleanup_paths()

        return True


class RunnerSingleJob(Runner):
    """Runner subclass to execute a single job"""

    def _get_node_from_commit(self, git_commit):
        nodes = self._db.get_nodes({
            "revision.commit": git_commit,
        })
        return nodes[0] if nodes else None

    def _setup(self, args):
        if args.node_id:
            checkout_node = self._db.get_node(args.node_id)
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
        job, tmp = self._schedule_test(node, plan, device)
        if self._runtime.config.lab_type == 'shell':
            self.log.info("Waiting...")
            job.wait()
            self.log.info("...done")
        return True


class cmd_loop(Command):
    help = "Listen to pub/sub events and run in a loop"
    args = [Args.db_config, Args.lab_config, Args.output]
    opt_args = [Args.verbose, Args.plan]

    def __call__(self, configs, args):
        return RunnerLoop(configs, args).run()


class cmd_run(Command):
    help = "Run one arbitrary test and exit"
    args = [
        Args.db_config, Args.lab_config, Args.output,
        Args.plan, Args.target,
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
