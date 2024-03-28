#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (c) Meta Platforms, Inc. and affiliates.
# Author: Nikolay Yurin <yurinnick@meta.com>

import os
import sys
import json
import requests
import time
import tempfile
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse
from urllib.request import urlopen

import kernelci
import kernelci.build
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts
import kernelci.storage

from base import Service
from tarball import Tarball


class Patchset(Tarball):
    TAR_CREATE_CMD = """\
set -e
cd {target_dir}
tar --create --transform "s/^/{prefix}\\//" * | gzip > {tarball_path}
"""

    APPLY_PATCH_SHELL_CMD = """\
set -e
cd {checkout_path}
patch -p1 < {patch_file}
"""

    # FIXME: I really don"t have a good idea what I"m doing here
    # This code probably needs rework and put into kernelci.patch
    def _hash_patch(self, patch_name, patch_file):
        allowed_prefixes = {
            b"old mode",  # Old file permissions
            b"new mode",  # New file permissions
            b"-",  # This convers both removed lines and source file
            b"+",  # This convers both added lines and target file
            # "@" I don"t know how we should handle hunks yet
        }
        hashable_patch_lines = []
        for line in patch_file.readlines():
            if not line:
                continue

            for prefix in allowed_prefixes:
                if line.startswith(prefix):
                    hashable_patch_lines.append(line)
                    break

        hashable_content = b"/n".join(hashable_patch_lines)
        self.log.debug(
            "Hashable content:\n" +
            hashable_content.decode("utf-8")
        )
        patch_hash_digest = hashlib.sha256(hashable_content).hexdigest()
        self.log.debug(f"Patch {patch_name} hash: {patch_hash_digest}")
        return patch_hash_digest

    # FIXME: move into kernelci.patch
    def _apply_patch(self, checkout_path, patch_name, patch_url):
        self.log.info(
            f"Applying patch {patch_name}, url: {patch_url}",
        )
        try:
            encoding = urlopen(patch_url).headers.get_charsets()[0]
        except Exception as e:
            self.log.warn(
                "Failed to fetch encoding from patch "
                f"{patch_name} headers: {e}"
            )
            self.log.warn("Falling back to utf-8 encoding")
            encoding = "utf-8"

        with tempfile.NamedTemporaryFile(
            prefix="{}-{}-".format(
                self._service_config.patchset_tmp_file_prefix,
                patch_name
            ),
            encoding=encoding
        ) as tmp_f:
            if not kernelci.build._download_file(patch_url, tmp_f.name):
                raise FileNotFoundError(
                    f"Error downloading patch from {patch_url}"
                )

            kernelci.shell_cmd(self.APPLY_PATCH_SHELL_CMD.format(
                checkout_path=checkout_path,
                patch_file=tmp_f.name,
            ))

            return self._hash_patch(patch_name, tmp_f)

    # FIXME: move into kernelci.patch
    def _apply_patches(self, checkout_path, patch_artifacts):
        patchset_hash = hashlib.sha256()
        for patch_name, patch_url in patch_artifacts.items():
            patch_hash = self._apply_patch(checkout_path, patch_name, patch_url)
            patchset_hash.update(patch_hash.encode("utf-8"))

        patchset_hash_digest = patchset_hash.hexdigest()
        self.log.debug(f"Patchset hash: {patchset_hash_digest}")
        return patchset_hash_digest

    def _download_checkout_archive(self, download_path, tarball_url, retries=3):
        self.log.info(f"Downloading checkout tarball, url: {tarball_url}")
        tar_filename = os.path.basename(urlparse(tarball_url).path)
        kernelci.build.pull_tarball(
            kdir=download_path,
            url=tarball_url,
            dest_filename=tar_filename,
            retries=retries,
            delete=True
        )

    def _update_node(
        self,
        patchset_node,
        checkout_node,
        tarball_url,
        patchset_hash
    ):
        patchset_data = checkout_node.get("data", {}).copy()
        patchset_data["kernel_revision"]["patchset"] = patchset_hash

        updated_node = patchset_node.copy()
        updated_node["artifacts"]["tarball"] = tarball_url
        updated_node["state"] = "available"
        updated_node["data"] = patchset_data
        updated_node["holdoff"] = str(
            datetime.utcnow() + timedelta(minutes=10)
        )

        try:
            self._api.node.update(updated_node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

    def _setup(self, *args):
        return self._api_helper.subscribe_filters({
            "op": "created",
            "name": "patchset",
            "state": "running",
        })

    def _has_allowed_domain(self, url):
        domain = urlparse(url).hostname
        if domain not in self._service_config.allowed_domains:
            raise RuntimeError(
                "Forbidden mbox domain %s, allowed domains: %s",
                domain,
                self._service_config.allowed_domains,
            )

    def _get_patch_artifacts(self, patchset_node):
        node_artifacts = patchset_node.get("artifacts")
        if not node_artifacts:
            raise ValueError(
                "Patchset node %s has no artifacts",
                patchset_node["id"],
            )

        for patch_mbox_url in node_artifacts.values():
            self._has_allowed_domain(patch_mbox_url)

        return node_artifacts

    def _gen_checkout_name(self, checkout_node):
        revision = checkout_node["data"]["kernel_revision"]
        return "-".join([
            "linux",
            revision["tree"],
            revision["branch"],
            revision["describe"],
        ])

    def _process_patchset(self, checkout_node, patchset_node):
        patch_artifacts = self._get_patch_artifacts(patchset_node)

        # Tarball download implicitely removes destination dir
        # there's no need to cleanup this directory
        self._download_checkout_archive(
            download_path=self._service_config.kdir,
            tarball_url=checkout_node["artifacts"]["tarball"]
        )

        checkout_name = self._gen_checkout_name(checkout_node)
        checkout_path = os.path.join(self._service_config.kdir, checkout_name)

        patchset_hash = self._apply_patches(checkout_path, patch_artifacts)
        patchset_hash_short = patchset_hash[
            :self._service_config.patchset_short_hash_len
        ]

        tarball_path = self._make_tarball(
            target_dir=checkout_path,
            tarball_name=f"{checkout_name}-{patchset_hash_short}"
        )
        tarball_url = self._push_tarball(tarball_path)

        self._update_node(
            patchset_node=patchset_node,
            checkout_node=checkout_node,
            tarball_url=tarball_url,
            patchset_hash=patchset_hash
        )

    def _mark_failed(self, patchset_node):
        node = patchset_node.copy()
        node.update({
            "state": "done",
            "result": "fail",
        })
        try:
            self._api.node.update(node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

    def _mark_failed_if_no_parent(self, patchset_node):
        if not patchset_node["parent"]:
            self.log.error(
                f"Patchset node {patchset_node['id']} as has no parent"
                "checkout node , marking node as failed",
            )
            self._mark_failed(patchset_node)
            return True

        return False

    def _mark_failed_if_parent_failed(self, patchset_node, checkout_node):
        if (
            checkout_node["state"] == "done" and
            checkout_node["result"] == "fail"
        ):
            self.log.error(
                f"Parent checkout node {checkout_node['id']} failed, "
                f"marking patchset node {patchset_node['id']} as failed",
            )
            self._mark_failed(patchset_node)
            return True

        return False

    def _run(self, _sub_id):
        self.log.info("Listening for new trigger events")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            patchset_nodes = self._api.node.find({
                "name": "patchset",
                "state": "running",
            })

            if patchset_nodes:
                self.log.debug(f"Found patchset nodes: {patchset_nodes}")

            for patchset_node in patchset_nodes:
                if self._mark_failed_if_no_parent(patchset_node):
                    continue

                checkout_node = self._api.node.get(patchset_node["parent"])

                if self._mark_failed_if_parent_failed(
                    patchset_node,
                    checkout_node
                ):
                    continue

                if checkout_node["state"] == "running":
                    self.log.info(
                        f"Patchset node {patchset_node['id']} is waiting "
                        f"for checkout node {checkout_node['id']} to complete",
                    )
                    continue

                try:
                    self.log.info(
                        f"Processing patchset node: {patchset_node['id']}",
                    )
                    self._process_patchset(checkout_node, patchset_node)
                except Exception as e:
                    self.log.error(
                        f"Patchset node {patchset_node['id']} "
                        f"processing failed: {e}",
                    )
                    self.log.traceback()
                    self._mark_failed(patchset_node)

            self.log.info(
                "Waiting %d seconds for a new nodes..." %
                self._service_config.polling_delay_secs,
            )
            time.sleep(self._service_config.polling_delay_secs)


class cmd_run(Command):
    help = (
        "Wait for a checkout node to be available "
        "and push a source+patchset tarball"
    )
    args = [
        Args.kdir, Args.output, Args.api_config, Args.storage_config,
    ]
    opt_args = [
        Args.verbose, Args.storage_cred,
    ]

    def __call__(self, configs, args):
        return Patchset(configs, args).run(args)


if __name__ == "__main__":
    opts = parse_opts("patchset", globals())
    configs = kernelci.config.load("config")
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
