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
from kernelci.legacy.cli import Args, Command, parse_opts
import kernelci.storage

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
        self._storage_config = configs['storage_configs'][args.storage_config]
        self._storage = kernelci.storage.get_storage(
            self._storage_config, args.storage_cred
        )

    def _find_build_config(self, node):
        revision = node['data']['kernel_revision']
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
        tarball_name = self._make_tarball(config, describe)
        tarball_path = os.path.join(self._output, tarball_name)
        self.log.info(f"Uploading {tarball_path}")
        tarball_url = self._storage.upload_single((tarball_path, tarball_name))
        self.log.info(f"Upload complete: {tarball_url}")
        os.unlink(tarball_path)
        return tarball_url

    def _get_version_from_describe(self):
        describe_v = kernelci.build.git_describe_verbose(self._kdir)
        version = KVER_RE.match(describe_v).groupdict()
        return {
            key: value
            for key, value in version.items()
            if value
        }

    def _update_node(self, checkout_node, describe, version, tarball_url):
        node = checkout_node.copy()
        node['data']['kernel_revision'].update({
            'describe': describe,
            'version': version,
        })
        node.update({
            'state': 'available',
            'artifacts': {
                'tarball': tarball_url,
            },
            'holdoff': str(datetime.utcnow() + timedelta(minutes=10))
        })
        try:
            self._api.node.update(node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

    def _setup(self, args):
        return self._api_helper.subscribe_filters({
            'op': 'created',
            'kind': 'checkout',
            'state': 'running',
        })

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)

    def _dry_run(self, checkout_node):
        self.log.info(f"Dry-run - node: {checkout_node['id']}")
        debug_params = checkout_node['debug']
        describe = debug_params.get('describe', 'v0.1.dry-run')
        version = self._get_version_from_describe()
        tarball_url = debug_params.get('tarball',
                                       'http://dry-run.test/tarball')
        self._update_node(checkout_node, describe, version, tarball_url)

    def _run(self, sub_id):
        self.log.info("Listening for new trigger events")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            checkout_node = self._api_helper.receive_event_node(sub_id)

            build_config = self._find_build_config(checkout_node)
            if build_config is None:
                continue

            if (checkout_node.get('debug') and
                checkout_node['debug'].get('dry_run')):  # noqa
                self._dry_run(checkout_node)
            else:
                self._update_repo(build_config)
                describe = kernelci.build.git_describe(
                    build_config.tree.name, self._kdir
                )
                version = self._get_version_from_describe()
                tarball_url = self._push_tarball(build_config, describe)
                self._update_node(checkout_node, describe,
                                  version, tarball_url)
        return True


class cmd_run(Command):
    help = "Wait for a new revision event and push a source tarball"
    args = [
        Args.kdir, Args.output, Args.api_config, Args.storage_config,
    ]
    opt_args = [
        Args.verbose, Args.storage_cred,
    ]

    def __call__(self, configs, args):
        return Tarball(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('tarball', globals())
    yaml_configs = opts.get_yaml_configs() or 'config/pipeline.yaml'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
