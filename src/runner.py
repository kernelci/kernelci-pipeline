#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import json
import logging
import os
import sys
import tempfile

import kernelci
import kernelci.config
import kernelci.db
import kernelci.lab
from kernelci.cli import Args, Command, parse_opts

from logger import Logger


class Runner:

    def __init__(self, configs, args):
        self._logger = Logger("config/logger.conf", "runner")
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._plan_configs = configs['test_plans']
        self._device_configs = configs['device_types']
        runtime_config = configs['labs'][args.lab_config]
        self._runtime = kernelci.lab.get_api(runtime_config)
        self._output = args.output
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        self._verbose = args.verbose
        self._job_tmp_dirs = {}

    def _create_node(self, tarball_node, plan_config):
        node = {
            'parent': tarball_node['parent'],
            'name': plan_config.name,
            'artifacts': tarball_node['artifacts'],
            'revision': tarball_node['revision'],
        }
        return self._db.submit({'node': node})[0]

    def _generate_job(self, node, plan_config, device_config, tmp):
        self._logger.log_message(logging.INFO, "Generating job")
        self._logger.log_message(logging.INFO, f"tmp: {tmp}")
        revision = node['revision']
        params = {
            'db_config_yaml': self._db_config.to_yaml(),
            'name': plan_config.name,
            'git_url': revision['url'],
            'git_commit': revision['commit'],
            'git_describe': revision['describe'],
            'node_id': node['_id'],
            'tarball_url': node['artifacts']['tarball'],
            'workspace': tmp,
        }
        params.update(plan_config.params)
        params.update(device_config.params)
        config_path = self._runtime.config.config_path
        templates_path = [
            os.path.join(path, config_path)
            for path in ['config', '/etc/kernelci']
        ]
        job = self._runtime.generate(
            params, device_config, plan_config,
            templates_path=templates_path
        )
        output_file = self._runtime.save_file(job, tmp, params)
        self._logger.log_message(logging.INFO, f"output_file: {output_file}")
        return output_file

    def _schedule_test(self, tarball_node, plan, device):
        self._logger.log_message(logging.INFO, "Tarball: {}".format(
            tarball_node['artifacts']['tarball']
        ))

        self._logger.log_message(logging.INFO, "Creating test node")
        node = self._create_node(tarball_node, plan)

        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = self._generate_job(node, plan, device, tmp.name)

        self._logger.log_message(logging.INFO, "Running test")
        job = self._runtime.submit(output_file)
        return job, tmp

    def _run_single_job(self, tarball_node, plan, device):
        try:
            job, tmp = self._schedule_test(tarball_node, plan, device)
            if self._runtime.config.lab_type == 'shell':
                self._logger.log_message(logging.INFO, "Waiting...")
                job.wait()
                self._logger.log_message(logging.INFO, "...done")
        except KeyboardInterrupt as e:
            self._logger.log_message(logging.ERROR, "Aborting.")
        finally:
            return True

    def _get_node_from_commit(self, git_commit):
        nodes = self._db.get_nodes_by_commit_hash(git_commit)
        return nodes[0] if nodes else None

    def run(self, args):
        if args.node_id:
            tarball_node = self._db.get_node(args.node_id)
        elif args.git_commit:
            tarball_node = self._get_node_from_commit(args.git_commit)
        else:
            tarball_node = None

        if tarball_node is None:
            self._logger.log_message(logging.ERROR, "Node not found")
            return False

        plan_config = self._plan_configs[args.plan]
        device_config = self._device_configs[args.target]
        return self._run_single_job(tarball_node, plan_config,
                                    device_config)

class RunnerLoop(Runner):
    """Runner subclass to execute in a loop"""

    def _cleanup_paths(self):
        job_tmp_dirs = {
            process: tmp
            for process, tmp in self._job_tmp_dirs.items()
            if process.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?

    def loop(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'op': 'created',
            'name': 'tarball',
            'status': 'pass',
        })
        self._logger.log_message(logging.INFO,
                                 "Listening for completed checkout events")
        self._logger.log_message(logging.INFO,
                                 "Press Ctrl-C to stop.")

        # ToDo: iterate over test configs
        plan = self._plan_configs['check-describe']

        # ToDo: iterate over device types for the current runtime
        if self._runtime.config.lab_type == 'shell':
            device = self._device_configs['shell_python']
        else:
            device = self._device_configs['kubernetes_python']

        try:
            while True:
                tarball_node = self._db.receive_node(sub_id)
                job, tmp = self._schedule_test(tarball_node, plan,
                                               device)
                if self._runtime.config.lab_type == 'shell':
                    self._job_tmp_dirs[job] = tmp
                self._cleanup_paths()
        except KeyboardInterrupt as e:
            self._logger.log_message(logging.INFO, "Stopping.")
        finally:
            self._db.unsubscribe(sub_id)
            self._cleanup_paths()
            return True


class cmd_loop(Command):
    help = "Listen to pub/sub events and run in a loop"
    args = [Args.db_config, Args.lab_config, Args.output]
    opt_args = [Args.verbose]

    def __call__(self, configs, args):
        return RunnerLoop(configs, args).loop()


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
            return Runner(configs, args).run(args)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return False


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
