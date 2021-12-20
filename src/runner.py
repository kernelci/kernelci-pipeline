#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import json
import os
import requests
import sys
import tempfile

import kernelci
import kernelci.config
import kernelci.data
import kernelci.lab
from kernelci.cli import Args, Command, parse_opts


def _arg_default(arg, default):
    arg_copy = arg.copy()
    arg_copy['default'] = default
    return arg_copy


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
        self._job_tmp_dirs = {}

    def run(self):
        sub_id = self._db.subscribe('node')
        self._print("Listening for new checkout events")
        self._print("Press Ctrl-C to stop.")

        try:
            while True:
                sys.stdout.flush()
                event = self._db.get_event(sub_id)
                if event.data['op'] != 'created':
                    continue

                obj = self._db.get_node_from_event(event)
                if obj['name'] != 'checkout':
                    continue

                self._print("Creating node")
                node = self._create_node(obj)

                tmp = tempfile.TemporaryDirectory(dir=self._output)
                self._print("Generating job")
                self._print(f"tmp: {tmp.name}")
                output_file = self._generate_job(node, tmp.name)
                self._print(f"output_file: {output_file}")

                self._print("Running test")
                process = self._runtime.submit(output_file, get_process=True)
                self._job_tmp_dirs[process] = tmp

                self._cleanup_paths()
        except KeyboardInterrupt as e:
            self._print("Stopping.")
        finally:
            self._db.unsubscribe(sub_id)

    def _print(self, msg):
        print(msg)
        sys.stdout.flush()

    def _create_node(self, obj):
        node = {
            'parent': obj['_id'],
            'name': self._plan_config.name,
            'revision': obj['revision'],
        }
        return self._db.submit({'node': node})[0]

    def _generate_job(self, node, tmp):
        revision = node['revision']
        params = {
            'name': self._plan_config.name,
            'git_url': revision['url'],
            'git_commit': revision['commit'],
            'git_describe': revision['describe'],
            'node_id': node['_id'],
        }
        params.update(self._plan_config.params)
        params.update(self._device_config.params)
        job = self._runtime.generate(
            params, self._device_config, self._plan_config,
            db_config=self._db_config
        )
        return self._runtime.save_file(job, tmp, params)

    def _cleanup_paths(self):
        job_tmp_dirs = {
            process: tmp
            for process, tmp in self._job_tmp_dirs.items()
            if process.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?


class cmd_run(Command):
    help = "Run some arbitrary test"
    args = [Args.db_config]
    opt_args = [
        _arg_default(Args.plan, 'check-describe'),
        _arg_default(Args.output, 'data'),
    ]

    def __call__(self, configs, args):
        runner = Runner(configs, args)
        runner.run()


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
