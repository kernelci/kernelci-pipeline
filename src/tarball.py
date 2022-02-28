#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import os
import sys
import urllib.parse

import kernelci
import kernelci.build
import kernelci.config
import kernelci.data
from kernelci.cli import Args, Command, parse_opts


class Tarball:

    def __init__(self, configs, args):
        self._build_configs = configs['build_configs']
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.data.get_db(self._db_config, api_token)
        self._kdir = args.kdir
        self._output = args.output
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        self._verbose = args.verbose
        self._ssh_key = args.ssh_key
        self._ssh_port = args.ssh_port
        self._ssh_user = args.ssh_user
        self._ssh_host = args.ssh_host
        self._storage_url = args.storage_url

    def _print(self, msg):
        print(msg)
        sys.stdout.flush()

    def _info(self, msg):
        if self._verbose:
            self._print(msg)

    def _find_build_config(self, node):
        revision = node['revision']
        tree = revision['tree']
        branch = revision['branch']
        for name, config in self._build_configs.items():
            if config.tree.name == tree and config.branch == branch:
                return config

    def _update_repo(self, config):
        self._info(f"Updating repo for {config.name}")
        kernelci.build.update_repo(config, self._kdir)
        self._info("Repo updated")

    def _make_tarball(self, config, describe):
        name = '-'.join(['linux', config.tree.name, config.branch, describe])
        tarball = f"{name}.tar.gz"
        self._info(f"Making tarball {tarball}")
        output_path = os.path.relpath(self._output, self._kdir)
        cmd = """\
set -e
cd {kdir}
git archive --format=tar --prefix={name}/ HEAD | gzip > {output}/{tarball}
""".format(kdir=self._kdir, name=name, output=output_path, tarball=tarball)
        self._info(cmd)
        kernelci.shell_cmd(cmd)
        self._info("Tarball created")
        return tarball

    def _push_tarball(self, config, describe):
        # ToDo: kernelci.build.make_tarball()
        tarball = self._make_tarball(config, describe)
        tarball_path = os.path.join(self._output, tarball)
        self._info(f"Uploading {tarball_path}")
        # ToDo: self._storage.upload()
        cmd = """\
scp \
  -i {key} \
  -P {port} \
  -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null \
  {tarball} \
  {user}@{host}:~/data/
""".format(key=self._ssh_key, port=self._ssh_port, user=self._ssh_user,
           host=self._ssh_host, tarball=tarball_path)
        kernelci.shell_cmd(cmd)
        self._info("Upload complete")
        os.unlink(tarball_path)
        return tarball

    def _update_node(self, node, describe, tarball, status):
        node['revision']['describe'] = describe
        node['status'] = status
        node['artifacts'] = {
            'tarball': urllib.parse.urljoin(self._storage_url, tarball),
        }
        self._db.submit({'node': node})

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

                node = self._db.get_node_from_event(event)
                if node['name'] != 'checkout':
                    continue

                if node['status'] != "pending":
                    continue

                build_config = self._find_build_config(node)
                if build_config is None:
                    continue

                self._update_repo(build_config)

                describe = kernelci.build.git_describe(
                    build_config.tree.name, self._kdir
                )
                tarball = self._push_tarball(build_config, describe)
                self._update_node(node, describe, tarball, "pass")
        except KeyboardInterrupt as e:
            self._print("Stopping.")
        finally:
            self._db.unsubscribe(sub_id)


class cmd_run(Command):
    help = "Wait for a new revision event and push a source tarball"
    args = [
        Args.kdir, Args.output, Args.db_config,
        {
            'name': '--ssh-key',
            'help': "Path to the ssh key for uploading to storage",
        },
        {
            'name': '--ssh-port',
            'help': "Storage SSH port number",
            'type': int,
        },
        {
            'name': '--ssh-user',
            'help': "Storage SSH user name",
        },
        {
            'name': '--ssh-host',
            'help': "Storage SSH host",
        },
        {
            'name': '--storage-url',
            'help': "Storage HTTP URL for downloads",
        },
    ]
    opt_args = [
        Args.verbose,
    ]

    def __call__(self, configs, args):
        Tarball(configs, args).run()
        return True


if __name__ == '__main__':
    opts = parse_opts('tarball', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
