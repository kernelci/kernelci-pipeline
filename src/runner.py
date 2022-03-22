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
        self._plan_config = configs['test_plans'][args.plan]
        self._device_config = configs['device_types']['shell_python']
        runtime_config = configs['labs'][args.lab_config]
        self._runtime = kernelci.lab.get_api(runtime_config)
        self._output = args.output
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        self._verbose = args.verbose
        self._job_tmp_dirs = {}
        self._node_id = args.node_id
        self._git_commit = args.git_commit

    def _create_node(self, checkout_node):
        node = {
            'parent': checkout_node['_id'],
            'name': self._plan_config.name,
            'artifacts': checkout_node['artifacts'],
            'revision': checkout_node['revision'],
        }
        return self._db.submit({'node': node})[0]

    def _generate_job(self, node, tmp):
        self._logger.log_message(logging.INFO, "Generating job")
        self._logger.log_message(logging.INFO, f"tmp: {tmp}")
        revision = node['revision']
        params = {
            'db_config_yaml': self._db_config.to_yaml(),
            'name': self._plan_config.name,
            'git_url': revision['url'],
            'git_commit': revision['commit'],
            'git_describe': revision['describe'],
            'node_id': node['_id'],
            'tarball_url': node['artifacts']['tarball'],
            'workspace': tmp,
        }
        params.update(self._plan_config.params)
        params.update(self._device_config.params)
        config_path = self._runtime.config.config_path
        templates_path = [
            os.path.join(path, config_path)
            for path in ['config', '/etc/kernelci']
        ]
        job = self._runtime.generate(
            params, self._device_config, self._plan_config,
            templates_path=templates_path
        )
        output_file = self._runtime.save_file(job, tmp, params)
        self._logger.log_message(logging.INFO, f"output_file: {output_file}")
        return output_file

    def _schedule_test(self, checkout_node):
        self._logger.log_message(logging.INFO, "Tarball: {}".format(
            checkout_node['artifacts']['tarball']
        ))

        self._logger.log_message(logging.INFO, "Creating test node")
        node = self._create_node(checkout_node)

        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = self._generate_job(node, tmp.name)

        self._logger.log_message(logging.INFO, "Running test")
        process = self._runtime.submit(output_file, get_process=True)
        return process, tmp

    def _cleanup_paths(self):
        job_tmp_dirs = {
            process: tmp
            for process, tmp in self._job_tmp_dirs.items()
            if process.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?

    def _get_node_from_commit(self, git_commit):
        nodes = self._db.get_nodes_by_commit_hash(git_commit)
        return nodes[0] if nodes else None

    def _run_single_job(self, checkout_node):
        try:
            process, tmp = self._schedule_test(checkout_node)
            self._logger.log_message(logging.INFO, "Waiting...")
            process.wait()
            self._logger.log_message(logging.INFO, "...done")
        except KeyboardInterrupt as e:
            self._logger.log_message(logging.ERROR, "Aborting.")
        finally:
            self._cleanup_paths()

    def _run_loop(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'op': 'updated',
            'name': 'checkout',
            'status': 'pass',
        })
        self._logger.log_message(logging.INFO,
                                 "Listening for completed checkout events")
        self._logger.log_message(logging.INFO,
                                 "Press Ctrl-C to stop.")

        try:
            while True:
                checkout_node = self._db.receive_node(sub_id)
                process, tmp = self._schedule_test(checkout_node)
                self._job_tmp_dirs[process] = tmp
                self._cleanup_paths()
        except KeyboardInterrupt as e:
            self._logger.log_message(logging.INFO, "Stopping.")
        finally:
            self._db.unsubscribe(sub_id)
            self._cleanup_paths()

    def run(self):
        if self._node_id:
            checkout_node = self._db.get_node(self._node_id)
            self._run_single_job(checkout_node)
        elif self._git_commit:
            checkout_node = self._get_node_from_commit(self._git_commit)
            self._run_single_job(checkout_node)
        else:
            self._run_loop()


class cmd_run(Command):
    help = "Run some arbitrary test"
    args = [Args.db_config, Args.lab_config]
    opt_args = [
        Args.plan, Args.output, Args.verbose,
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
        Runner(configs, args).run()
        return True


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
