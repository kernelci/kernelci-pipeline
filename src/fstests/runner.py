#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Alexandra Pereira <alexandra.pereira@collabora.com>

import os
import sys

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

    def run(self):
        sub_id = self._db.subscribe_node_channel({
            'op': 'created',
            'name': 'tarball',
        })
        print('Listening for tarballs')
        print('Press Ctrl-C to stop.')
        tarball_node = None
        try:
            while True:
                tarball_node = self._db.receive_node(sub_id)
                print(f"Node tarball with id: {tarball_node['_id']}\
                    from revision: {tarball_node['revision']['commit'][:12]}")
        except KeyboardInterrupt as e:
            print('Stopping.')
        except Exception as e:
            print('Error', e)
        finally:
            self._db.unsubscribe(sub_id)
            return True


class cmd_run(Command):
    help = 'KVM fstests runner'
    args = [Args.db_config]
    opt_args = [Args.verbose]

    def __call__(self, configs, args):
        return FstestsRunner(configs, args).run()


if __name__ == '__main__':
    opts = parse_opts('fstests_runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
