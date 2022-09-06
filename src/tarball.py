#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import logging
import os
import re
import sys
import traceback
import urllib.parse

import kernelci
import kernelci.build
import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts

from logger import Logger

KVER_RE = re.compile(
    r'^v(?P<version>[\d]+)\.'
    r'(?P<patchlevel>[\d]+)'
    r'(\.(?P<sublevel>[\d]+))?'
    r'(?P<extra>.*)?'
)


class Tarball:

    def __init__(self, configs, args):
        self._build_configs = configs['build_configs']
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
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
        self._logger = Logger("config/logger.conf", "tarball")

    def _find_build_config(self, node):
        revision = node['revision']
        tree = revision['tree']
        branch = revision['branch']
        for name, config in self._build_configs.items():
            if config.tree.name == tree and config.branch == branch:
                return config

    def _update_repo(self, config):
        self._logger.log_message(logging.INFO,
                                 f"Updating repo for {config.name}")
        kernelci.build.update_repo(config, self._kdir)
        self._logger.log_message(logging.INFO, "Repo updated")

    def _make_tarball(self, config, describe):
        name = '-'.join(['linux', config.tree.name, config.branch, describe])
        tarball = f"{name}.tar.gz"
        self._logger.log_message(logging.INFO, f"Making tarball {tarball}")
        output_path = os.path.relpath(self._output, self._kdir)
        cmd = """\
set -e
cd {kdir}
git archive --format=tar --prefix={name}/ HEAD | gzip > {output}/{tarball}
""".format(kdir=self._kdir, name=name, output=output_path, tarball=tarball)
        self._logger.log_message(logging.INFO, cmd)
        kernelci.shell_cmd(cmd)
        self._logger.log_message(logging.INFO, "Tarball created")
        return tarball

    def _push_tarball(self, config, describe):
        # ToDo: kernelci.build.make_tarball()
        tarball = self._make_tarball(config, describe)
        tarball_path = os.path.join(self._output, tarball)
        self._logger.log_message(logging.INFO, f"Uploading {tarball_path}")
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
        self._logger.log_message(logging.INFO, "Upload complete")
        os.unlink(tarball_path)
        return tarball

    def _create_tarball_node(self, checkout_node):
        tarball_node = {
            'parent': checkout_node['_id'],
            'name': 'tarball',
            'path': checkout_node['path'] + ['tarball'],
            'revision': checkout_node['revision'],
        }
        return self._db.submit({'node': tarball_node})[0]

    def _make_tarball_node_available(self, tarball_node, tarball):
        tarball_node.update({
            'state': 'available',
            'artifacts': {
                'tarball': urllib.parse.urljoin(self._storage_url, tarball),
            },
        })
        return self._db.submit({'node': tarball_node})

    def _add_checkout_version(self, node, describe, version):
        node['revision'].update({
            'describe': describe,
            'version': version,
        })
        return self._db.submit({'node': node})

    def _get_version_from_describe(self):
        describe_v = kernelci.build.git_describe_verbose(self._kdir)
        version = KVER_RE.match(describe_v).groupdict()
        return {
            key: value
            for key, value in version.items()
            if value
        }

    def run(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'op': 'created',
            'name': 'checkout',
            'state': 'available',
        })
        self._logger.log_message(logging.INFO,
                                 "Listening for new checkout events")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                checkout_node = self._db.receive_node(sub_id)

                build_config = self._find_build_config(checkout_node)
                if build_config is None:
                    continue

                tarball_node = self._create_tarball_node(checkout_node)
                self._update_repo(build_config)
                describe = kernelci.build.git_describe(
                    build_config.tree.name, self._kdir
                )
                version = self._get_version_from_describe()
                self._add_checkout_version(checkout_node, describe, version)
                tarball = self._push_tarball(build_config, describe)
                self._make_tarball_node_available(tarball_node, tarball)
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())
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
