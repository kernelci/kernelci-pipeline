#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Jeny Sadadia
# Author: Jeny Sadadia <jeny.sadadia@gmail.com>

import os
import sys

import kernelci.config
import kernelci.data
from kernelci.cli import Args, Command, parse_opts
import jinja2


class TestReport:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.data.get_db(self._db_config, api_token)

    def run(self):
        sub_id = self._db.subscribe('node')
        self._print("Listening for test completion events")
        self._print("Press Ctrl-C to stop.")

        try:
            while True:
                sys.stdout.flush()
                event = self._db.get_event(sub_id)

                node = self._db.get_node_from_event(event)
                if node['status'] == 'pending':
                    continue

                root_node = self._db.get_root_node(node['_id'])
                templateEnv = jinja2.Environment(
                            loader=jinja2.FileSystemLoader("./config/reports/")
                        )
                template = templateEnv.get_template("test-report.jinja2")
                self._print(template.render(total_runs=1, total_failures=0,
                                            root=root_node, tests=[node]))

        except KeyboardInterrupt as e:
            self._print("Stopping.")
        finally:
            self._db.unsubscribe(sub_id)

    def _print(self, msg):
        print(msg)
        sys.stdout.flush()


class cmd_run(Command):
    help = "Generate test report"
    args = [Args.db_config]

    def __call__(self, configs, args):
        generate_test_report = TestReport(configs, args)
        generate_test_report.run()


if __name__ == '__main__':
    opts = parse_opts('test_report', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
