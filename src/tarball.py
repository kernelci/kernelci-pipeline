#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

from datetime import datetime, timedelta
import logging
import os
import re
import sys
import urllib.parse
import json
import requests

import kernelci
import kernelci.build
import kernelci.config
from kernelci.cli import Args, Command, parse_opts

from base import Service

KVER_RE = re.compile(
    r'^v(?P<version>[\d]+)\.'
    r'(?P<patchlevel>[\d]+)'
    r'(\.(?P<sublevel>[\d]+))?'
    r'(?P<extra>.*)?'
)


class Tarball(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'tarball')
        self._build_configs = configs['build_configs']
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

    def _find_build_config(self, node):
        revision = node['revision']
        tree = revision['tree']
        branch = revision['branch']
        for name, config in self._build_configs.items():
            if config.tree.name == tree and config.branch == branch:
                return config

    def _update_repo(self, config):
        self.log.info(f"Updating repo for {config.name}")
        kernelci.build.update_repo(config, self._kdir)
        self.log.info("Repo updated")

    def _make_tarball(self, config, describe):
        name = '-'.join(['linux', config.tree.name, config.branch, describe])
        tarball = f"{name}.tar.gz"
        self.log.info(f"Making tarball {tarball}")
        output_path = os.path.relpath(self._output, self._kdir)
        cmd = """\
set -e
cd {kdir}
git archive --format=tar --prefix={name}/ HEAD | gzip > {output}/{tarball}
""".format(kdir=self._kdir, name=name, output=output_path, tarball=tarball)
        self.log.info(cmd)
        kernelci.shell_cmd(cmd)
        self.log.info("Tarball created")
        return tarball

    def _push_tarball(self, config, describe):
        # ToDo: kernelci.build.make_tarball()
        tarball = self._make_tarball(config, describe)
        tarball_path = os.path.join(self._output, tarball)
        self.log.info(f"Uploading {tarball_path}")
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
        self.log.info("Upload complete")
        os.unlink(tarball_path)
        return tarball

    def _get_version_from_describe(self):
        describe_v = kernelci.build.git_describe_verbose(self._kdir)
        version = KVER_RE.match(describe_v).groupdict()
        return {
            key: value
            for key, value in version.items()
            if value
        }

    def _update_node(self, checkout_node, describe, version, tarball):
        node = checkout_node.copy()
        node['revision'].update({
            'describe': describe,
            'version': version,
        })
        node.update({
            'state': 'available',
            'artifacts': {
                'tarball': urllib.parse.urljoin(self._storage_url, tarball),
            },
            'holdoff': str(datetime.utcnow() + timedelta(minutes=10))
        })
        try:
            self._api.submit({'node': node})
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

    def _setup(self, args):
        return self._api.subscribe_node_channel(filters={
            'op': 'created',
            'name': 'checkout',
            'state': 'running',
        })

    def _stop(self, sub_id):
        if sub_id:
            self._api.unsubscribe(sub_id)

    def _run(self, sub_id):
        self.log.info("Listening for new trigger events")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            checkout_node = self._api.receive_node(sub_id)

            build_config = self._find_build_config(checkout_node)
            if build_config is None:
                continue

            self._update_repo(build_config)
            describe = kernelci.build.git_describe(
                build_config.tree.name, self._kdir
            )
            version = self._get_version_from_describe()
            tarball = self._push_tarball(build_config, describe)
            self._update_node(checkout_node, describe, version, tarball)

        return True


class cmd_run(Command):
    help = "Wait for a new revision event and push a source tarball"
    args = [
        Args.kdir, Args.output, Args.api_config,
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
        return Tarball(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('tarball', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
