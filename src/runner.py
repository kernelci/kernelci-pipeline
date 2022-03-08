#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import json
import os
import sys
import tempfile

import kernelci
import kernelci.config
import kernelci.data
import kernelci.lab
from kernelci.cli import Args, Command, parse_opts


class Runner:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.data.get_db(self._db_config, api_token)
        self._plan_config = configs['test_plans'][args.plan]
        self._device_config = configs['device_types']['python']
        runtime_config = configs['labs']['shell']
        self._runtime = kernelci.lab.get_api(runtime_config)
        self._output = args.output
        self._verbose = args.verbose
        self._job_tmp_dirs = {}

    def _print(self, msg):
        print(msg)
        sys.stdout.flush()

    def _info(self, msg):
        if self._verbose:
            self._print(msg)

    def _create_node(self, checkout_node):
        node = {
            'parent': checkout_node['_id'],
            'name': self._plan_config.name,
            'artifacts': checkout_node['artifacts'],
            'revision': checkout_node['revision'],
        }
        return self._db.submit({'node': node})[0]

    def _generate_job(self, node, tmp):
        self._print("Generating job")
        self._info(f"tmp: {tmp}")
        revision = node['revision']
        params = {
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
        job = self._runtime.generate(
            params, self._device_config, self._plan_config,
            db_config=self._db_config
        )
        output_file = self._runtime.save_file(job, tmp, params)
        self._info(f"output_file: {output_file}")
        return output_file

    def _cleanup_paths(self):
        job_tmp_dirs = {
            process: tmp
            for process, tmp in self._job_tmp_dirs.items()
            if process.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?

    def run(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'op': 'updated',
            'name': 'checkout',
            'status': 'pass',
        })
        self._print("Listening for completed checkout events")
        self._print("Press Ctrl-C to stop.")

        try:
            while True:
                checkout_node = self._db.receive_node(sub_id)

                self._info("Tarball: {}".format(
                    checkout_node['artifacts']['tarball']
                ))

                self._print("Creating test node")
                node = self._create_node(checkout_node)

                tmp = tempfile.TemporaryDirectory(dir=self._output)
                output_file = self._generate_job(node, tmp.name)

                self._print("Running test")
                process = self._runtime.submit(output_file, get_process=True)
                self._job_tmp_dirs[process] = tmp

                self._cleanup_paths()
        except KeyboardInterrupt as e:
            self._print("Stopping.")
        finally:
            self._db.unsubscribe(sub_id)
            self._cleanup_paths()


class cmd_run(Command):
    help = "Run some arbitrary test"
    args = [Args.db_config, Args.verbose]
    opt_args = [
        Args.plan, Args.output,
    ]

    def __call__(self, configs, args):
        Runner(configs, args).run()
        return True


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
