#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>
# Author: Nikolay Yurin <yurinnick@meta.com>

from datetime import datetime, timedelta
import os
import re
import sys
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
    TAR_CREATE_CMD = """\
set -e
cd {target_dir}
git archive --format=tar --prefix={prefix}/ HEAD | gzip > {tarball_path}
"""

    def __init__(self, global_configs, service_config):
        super().__init__(global_configs, service_config, 'tarball')
        self._service_config = service_config
        self._build_configs = global_configs['build_configs']
        if not os.path.exists(self._service_config.output):
            os.makedirs(self._service_config.output)
        storage_config = global_configs['storage_configs'][
            service_config.storage_config
        ]
        self._storage = kernelci.storage.get_storage(
            storage_config, service_config.storage_cred
        )

    def _find_build_config(self, node):
        revision = node['data']['kernel_revision']
        tree = revision['tree']
        branch = revision['branch']
        for config in self._build_configs.values():
            if config.tree.name == tree and config.branch == branch:
                return config

    def _find_build_commit(self, node):
        revision = node['data'].get('kernel_revision')
        commit = revision.get('commit')
        return commit

    def _checkout_commitid(self, commitid):
        self.log.info(f"Checking out commit {commitid}")
        # i might need something from kernelci.build
        # but i prefer to implement it myself
        cwd = os.getcwd()
        os.chdir(self._service_config.kdir)
        kernelci.shell_cmd(f"git checkout {commitid}", self._service_config.kdir)
        os.chdir(cwd)
        self.log.info("Commit checked out")

    def _update_repo(self, config):
        '''
        Return True - if failed to update repo and need to retry
        Return False - if repo updated successfully
        '''
        self.log.info(f"Updating repo for {config.name}")
        try:
            kernelci.build.update_repo(config, self._service_config.kdir)
        except Exception as err:
            self.log.error(f"Failed to update: {err}, cleaning stale repo")
            # safeguard, make sure it is git repo
            if not os.path.exists(
                os.path.join(self._service_config.kdir, '.git')
            ):
                err_msg = f"{self._service_config.kdir} is not a git repo"
                self.log.error(err_msg)
                raise Exception(err_msg)
            # cleanup the repo and return True, so we try again
            kernelci.shell_cmd(f"rm -rf {self._service_config.kdir}")
            return True

        self.log.info("Repo updated")
        return False

    def _make_tarball(self, target_dir, tarball_name):
        self.log.info(f"Making tarball {tarball_name}")
        tarball_path = os.path.join(
            self._service_config.output,
            f"{tarball_name}.tar.gz"
        )
        cmd = self.TAR_CREATE_CMD.format(
            target_dir=target_dir,
            prefix=tarball_name,
            tarball_path=tarball_path
        )
        self.log.info(cmd)
        kernelci.shell_cmd(cmd)
        self.log.info("Tarball created")
        return tarball_path

    def _push_tarball(self, tarball_path):
        tarball_name = os.path.basename(tarball_path)
        self.log.info(f"Uploading {tarball_path}")
        tarball_url = self._storage.upload_single((tarball_path, tarball_name))
        self.log.info(f"Upload complete: {tarball_url}")
        os.unlink(tarball_path)
        return tarball_url

    def _get_version_from_describe(self):
        describe_v = kernelci.build.git_describe_verbose(
            self._service_config.kdir
        )
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

    def _update_failed_checkout_node(self, checkout_node, error_code, error_msg):
        node = checkout_node.copy()
        node.update({
            'state': 'done',
            'result': 'fail',
        })
        if 'data' not in node:
            node['data'] = {}
        node['data']['error_code'] = error_code
        node['data']['error_msg'] = error_msg
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

    def _run(self, sub_id):
        self.log.info("Listening for new trigger events")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            checkout_node, _ = self._api_helper.receive_event_node(sub_id)

            build_config = self._find_build_config(checkout_node)
            if build_config is None:
                continue

            if self._update_repo(build_config):
                self.log.error("Failed to update repo, retrying")
                if self._update_repo(build_config):
                    # critical failure, something wrong with git
                    self.log.error("Failed to update repo again, exit")
                    # Set checkout node result to fail
                    self._update_failed_checkout_node(checkout_node,
                                                      'git_checkout_failure',
                                                      'Failed to init/update git repo')
                    os._exit(1)

            commitid = self._find_build_commit(checkout_node)
            if commitid is None:
                self.log.error("Failed to find commit id")
                self._update_failed_checkout_node(checkout_node,
                                                  'git_checkout_failure',
                                                  'Failed to find commit id')
                os._exit(1)
            self._checkout_commitid(commitid)

            describe = kernelci.build.git_describe(
                build_config.tree.name, self._service_config.kdir
            )
            version = self._get_version_from_describe()
            tarball_name = '-'.join([
                'linux',
                build_config.tree.name,
                build_config.branch,
                describe
            ])
            tarball_path = self._make_tarball(
                self._service_config.kdir,
                tarball_name
            )
            tarball_url = self._push_tarball(tarball_path)
            self._update_node(checkout_node, describe, version, tarball_url)


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
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
