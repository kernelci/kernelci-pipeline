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

import kernelci
import kernelci.config
import kernelci.data
import kernelci.lab
from kernelci.cli import Args, Command, parse_opts


def _arg_default(arg, default):
    arg_copy = arg.copy()
    arg_copy['default'] = default
    return arg_copy


class cmd_run(Command):
    help = "Run some arbitrary test"
    args = [Args.db_config]
    opt_args = [
        _arg_default(Args.plan, 'check-describe'),
        _arg_default(Args.output, 'data'),
    ]

    def __call__(self, configs, args):
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.data.get_db(db_config, api_token)
        plan_config = configs['test_plans'][args.plan]
        target_config = configs['device_types']['python']
        runtime_config = configs['labs']['shell']
        runtime = kernelci.lab.get_api(runtime_config)

        sub_id = db.subscribe('node')
        print("Listening for new checkout events")
        print("Press Ctrl-C to stop.")
        sys.stdout.flush()

        try:
            while True:
                sys.stdout.flush()
                event = db.get_event(sub_id)
                if event.data['op'] != 'created':
                    continue

                obj = db.get_node_from_event(event)
                if obj['name'] != 'checkout':
                    continue

                print("Creating node")
                sys.stdout.flush()
                node = self._create_node(db, obj, args.plan)

                revision = node['revision']
                params = {
                    'name': plan_config.name,
                    'git_url': revision['url'],
                    'git_commit': revision['commit'],
                    'git_describe': revision['describe'],
                    'node_id': node['_id'],
                }
                params.update(plan_config.params)
                params.update(target_config.params)
                job = runtime.generate(params, target_config, plan_config,
                                       db_config=db_config)
                output_file = runtime.save_file(job, args.output, params)

                print("Running test")
                sys.stdout.flush()
                res = runtime.submit(output_file)
        except KeyboardInterrupt as e:
            print("Stopping.")
        finally:
            db.unsubscribe(sub_id)

        sys.stdout.flush()

    def _create_node(self, db, obj, plan_name):
        node = {
            'parent': obj['_id'],
            'name': plan_name,
            'revision': obj['revision'],
        }
        return db.submit({'node': node})[0]


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
