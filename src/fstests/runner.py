#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Alexandra Pereira <alexandra.pereira@collabora.com>

import os
import sys
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
        self._xfstests_bld_path = args.xfstests_bld_path
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        runtime_config = configs['labs']['shell']
        self._runtime = kernelci.lab.get_api(runtime_config)

    def _create_node(self, tarball_node, plan_config):
        node = {
            'parent': tarball_node['_id'],
            'name': plan_config.name,
            'artifacts': tarball_node['artifacts'],
            'revision': tarball_node['revision'],
            'path': tarball_node['path'] + [plan_config.name],
        }
        return self._db.submit({'node': node}, True)[0]

    def _schedule_job(self, tarball_node, device_config, tmp):
        node = self._create_node(tarball_node, self._plan)
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
                'xfstests_bld_path' : self._xfstests_bld_path
            }
            params.update(self._plan.params)
            params.update(device_config.params)
            templates = ['config/runtime',
                         '/etc/kernelci/runtime',
                         'src/fstests']
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

    def run(self, args):
        sub_id = None
        if not args.node_id:
            sub_id = self._db.subscribe_node_channel(filters={
                'name': 'checkout',
                'state': 'available',
            })
            print('Listening for checkouts')
            print('Press Ctrl-C to stop.')
        try:
            if sub_id:
                while True:
                    tarball_node = self._db.receive_node(sub_id)
                    print(f"Node tarball with id: {tarball_node['_id']}\
                        from revision: {tarball_node['revision']['commit'][:12]}")
                    device = self._device_configs['shell']
                    self._run_single_job(tarball_node, device)
            else:
                tarball_node = self._db.get_node(args.node_id)
                device = self._device_configs['shell']
                self._run_single_job(tarball_node, device)
        except KeyboardInterrupt as e:
            print('Stopping.')
        except Exception as e:
            print('Error', e)
        finally:
            if sub_id:
                self._db.unsubscribe(sub_id)
            return True


class cmd_run(Command):
    help = 'KVM fstests runner'
    args = [
        Args.db_config, Args.output,
        {
            'name': '--xfstests-bld-path',
            'help': "xfstests build directory"
        },
    ]
    opt_args = [
        Args.verbose,
        {
            'name': '--node-id',
            'help': "id of the checkout node rather than pub/sub",
        },
    ]

    def __call__(self, configs, args):
        return FstestsRunner(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('fstests_runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
