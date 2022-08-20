#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Alexandra Pereira <alexandra.pereira@collabora.com>

import os
import sys
import subprocess
import tempfile

import kernelci
import kernelci.config
import kernelci.db
import kernelci.lab
from kernelci.cli import Args, Command, parse_opts


class FstestsRunner:
    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._device_configs = configs['device_types']
        self._plan = configs['test_plans']['fstests']
        self._output = args.output
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        runtime_config = configs['labs']['shell']
        self._runtime = kernelci.lab.get_api(runtime_config)

    def _schedule_job(self, node, device_config, tmp):
        revision = node['revision']
        try:
            params = {
                'db_config_yaml': self._db_config.to_yaml(),
                'name': self._plan.name,
                'node_id': node['_id'],
                'revision': revision,
                'runtime': self._runtime.config.lab_type,
                'tarball_url': node['artifacts']['tarball'],
                'workspace': tmp,
            }
            params.update(self._plan.params)
            params.update(device_config.params)
            templates = ['config/runtime',
                         '/etc/kernelci/runtime']
            job = self._runtime.generate(
                params, device_config, self._plan, templates_path=templates
            )
            output_file = self._runtime.save_file(job, tmp, params)
            job_result = self._runtime.submit(output_file)
        except Exception as e:
            print(e)
        return job_result

    def _run_single_job(self, tarball_node, device):
        try:
            tmp = tempfile.TemporaryDirectory(dir=self._output)
            job = self._schedule_job(tarball_node, device, tmp.name)
            print("Waiting...")
            job.wait()
            print("...done")
        except KeyboardInterrupt as e:
            print("Aborting.")
        finally:
            return True

    def run(self):
        sub_id = self._db.subscribe_node_channel({
            'op': 'created',
            'name': 'tarball',
        })
        print('Listening for tarballs')
        print('Press Ctrl-C to stop.')
        try:
            while True:
                tarball_node = self._db.receive_node(sub_id)
                print(f"Node tarball with id: {tarball_node['_id']}\
                   from revision: {tarball_node['revision']['commit'][:12]}")
                device = self._device_configs['shell']
                self._run_single_job(tarball_node, device)
        except KeyboardInterrupt as e:
            print('Stopping.')
        except Exception as e:
            print('Error', e)
        finally:
            self._db.unsubscribe(sub_id)
            return True


class cmd_run(Command):
    help = 'KVM fstests runner'
    args = [
        Args.db_config, Args.output,
    ]
    opt_args = [Args.verbose]

    def __call__(self, configs, args):
        return FstestsRunner(configs, args).run()


if __name__ == '__main__':
    opts = parse_opts('fstests_runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
