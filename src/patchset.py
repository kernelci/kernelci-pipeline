#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (c) Meta Platforms, Inc. and affiliates.
# Author: Nikolay Yurin <yurinnick@meta.com>

import copy
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import urlparse

import kernelci
import kernelci.build
import kernelci.config
import requests
from kernelci.legacy.cli import Args, Command, parse_opts

from tarball import Tarball

DEFAULT_MAX_PATCH_SIZE_MB = 10

HUNK_HEADER_RE = re.compile(rb"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
GIT_DIFF_RE = re.compile(rb"^diff --git a/(\S+) b/(\S+)$")
RENAME_COPY_RE = re.compile(rb"^(?:rename|copy) (?:from|to) (.+)$")
MODE_LINE_RE = re.compile(
    rb"^(?:old mode|new mode|new file mode|deleted file mode) (\d+)$"
)


class PatchValidationError(Exception):
    """Raised when a patch fails safety validation"""


class Patchset(Tarball):
    # The checkout sources come from an extracted tarball, not a git
    # repository, so `git archive` can't be used here
    TAR_CREATE_CMD = """\
set -e
cd {target_dir}
tar --create --transform "s/^/{prefix}\\//" * | gzip > {tarball_path}
"""

    # --unified disables patch(1) format auto-detection, so ed-style
    # scripts (CVE-2018-1000156) can never be interpreted; --force keeps
    # it non-interactive and --no-backup-if-mismatch avoids .orig files
    # ending up in the released tarball
    APPLY_PATCH_SHELL_CMD = """\
set -e
cd {checkout_path}
patch -p1 --unified --force --no-backup-if-mismatch < {patch_file}
"""

    def _hash_patch(self, patch_name, patch_file):
        allowed_prefixes = {
            b"old mode",  # Old file permissions
            b"new mode",  # New file permissions
            b"-",  # This covers both removed lines and source file
            b"+",  # This covers both added lines and target file
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

        hashable_content = b"\n".join(hashable_patch_lines)
        patch_hash_digest = hashlib.sha256(hashable_content).hexdigest()
        self.log.debug(f"Patch {patch_name} hash: {patch_hash_digest}")
        return patch_hash_digest

    def _validate_patch_path(self, raw_path, checkout_path, strip=1):
        """Check that a file path referenced by a patch stays inside the
        checkout source tree once `patch -p1` strips its first component"""
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchValidationError(
                f"Undecodable file path in patch: {raw_path!r}"
            )
        # Timestamps after a tab are legal in ---/+++ header lines
        path = path.split("\t")[0].rstrip("\r")
        if path == "/dev/null":
            return
        if path.startswith('"'):
            raise PatchValidationError(f"Quoted file path not allowed: {path}")
        if path.startswith("/"):
            raise PatchValidationError(
                f"Absolute file path not allowed: {path}"
            )
        parts = path.split("/")
        if ".." in parts:
            raise PatchValidationError(f"Path traversal not allowed: {path}")
        target_parts = parts[strip:]
        if not target_parts or not all(target_parts):
            raise PatchValidationError(f"Invalid file path: {path}")
        if target_parts[0] == ".git":
            raise PatchValidationError(f"Patching .git is not allowed: {path}")
        # Resolve symlinks to catch in-tree links pointing outside the tree
        checkout_real = os.path.realpath(checkout_path)
        target_real = os.path.realpath(
            os.path.join(checkout_path, *target_parts)
        )
        if not target_real.startswith(checkout_real + os.sep):
            raise PatchValidationError(f"Path escapes the source tree: {path}")

    @staticmethod
    def _skip_hunk_body(lines, i, hunk_match):
        """Skip past a unified diff hunk body, returning the next index"""
        old_count = int(hunk_match.group(1) or 1)
        new_count = int(hunk_match.group(2) or 1)
        while (old_count > 0 or new_count > 0) and i < len(lines):
            first = lines[i][:1]
            if first == b"-":
                old_count -= 1
            elif first == b"+":
                new_count -= 1
            elif first != b"\\":
                # Context line; "\ No newline at end of file" markers
                # don't count towards either side
                old_count -= 1
                new_count -= 1
            i += 1
        return i

    def _validate_header_line(self, line, patch_name, checkout_path):
        """Validate a single line outside hunk bodies

        Returns the number of file headers found on this line.
        """
        if line.startswith(b"GIT binary patch") or line.startswith(
            b"Binary files "
        ):
            raise PatchValidationError(f"Binary patch content in {patch_name}")
        mode = MODE_LINE_RE.match(line)
        if mode and mode.group(1)[:2] in (b"12", b"16"):
            raise PatchValidationError(
                f"Symlink or submodule mode not allowed in {patch_name}"
            )
        git_diff = GIT_DIFF_RE.match(line)
        if git_diff:
            # The a/ and b/ prefixes are already consumed by the regex
            self._validate_patch_path(git_diff.group(1), checkout_path, strip=0)
            self._validate_patch_path(git_diff.group(2), checkout_path, strip=0)
            return 1
        if line.startswith(b"diff --git"):
            raise PatchValidationError(
                f"Unparseable diff header in {patch_name}: {line!r}"
            )
        rename_copy = RENAME_COPY_RE.match(line)
        if rename_copy:
            self._validate_patch_path(
                rename_copy.group(1), checkout_path, strip=0
            )
        return 0

    def _validate_patch(self, patch_name, patch_data, checkout_path):
        """Reject binary or malicious patches before they reach patch(1)

        Only text-only unified diffs are accepted and every file path
        they reference must stay inside the checkout source tree.
        """
        max_size = (
            1024
            * 1024
            * int(
                self._service_config.patchset_max_patch_size_mb
                or DEFAULT_MAX_PATCH_SIZE_MB
            )
        )
        if len(patch_data) > max_size:
            raise PatchValidationError(
                f"Patch {patch_name} exceeds maximum size of {max_size} bytes"
            )
        if b"\x00" in patch_data:
            raise PatchValidationError(
                f"Patch {patch_name} contains binary data"
            )

        lines = patch_data.split(b"\n")
        file_headers = 0
        i = 0
        while i < len(lines):
            line = lines[i]
            hunk = HUNK_HEADER_RE.match(line)
            if hunk:
                # Consume the hunk body so its content (which may itself
                # look like diff headers) is not misparsed
                i = self._skip_hunk_body(lines, i + 1, hunk)
                continue
            file_headers += self._validate_header_line(
                line, patch_name, checkout_path
            )
            if (
                line.startswith(b"--- ")
                and i + 1 < len(lines)
                and lines[i + 1].startswith(b"+++ ")
            ):
                file_headers += 1
                self._validate_patch_path(line[4:], checkout_path)
                self._validate_patch_path(lines[i + 1][4:], checkout_path)
                i += 2
                continue
            i += 1

        if not file_headers:
            raise PatchValidationError(
                f"No unified diff content found in {patch_name}"
            )

    def _apply_patch(self, checkout_path, patch_name, patch_url):
        self.log.info(f"Applying patch {patch_name}, url: {patch_url}")
        with tempfile.NamedTemporaryFile(
            prefix="{}-{}-".format(
                self._service_config.patchset_tmp_file_prefix, patch_name
            ),
        ) as tmp_f:
            if not kernelci.build._download_file(patch_url, tmp_f.name):
                raise FileNotFoundError(
                    f"Error downloading patch from {patch_url}"
                )

            self._validate_patch(patch_name, tmp_f.read(), checkout_path)

            kernelci.shell_cmd(
                self.APPLY_PATCH_SHELL_CMD.format(
                    checkout_path=checkout_path,
                    patch_file=tmp_f.name,
                )
            )

            tmp_f.seek(0)
            return self._hash_patch(patch_name, tmp_f)

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
        if not kernelci.build.pull_tarball(
            kdir=download_path,
            url=tarball_url,
            dest_filename=os.path.join(
                self._service_config.output, tar_filename
            ),
            retries=retries,
            delete=True,
        ):
            raise RuntimeError(
                f"Failed to download checkout tarball {tarball_url}"
            )

    def _update_node(
        self, patchset_node, checkout_node, tarball_url, patchset_hash
    ):
        patchset_data = copy.deepcopy(checkout_node.get("data", {}))
        patchset_data["kernel_revision"]["patchset"] = patchset_hash

        updated_node = patchset_node.copy()
        updated_node["artifacts"]["tarball"] = tarball_url
        updated_node["state"] = "available"
        updated_node["result"] = "pass"
        updated_node["data"] = patchset_data
        updated_node["holdoff"] = str(
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10)
        )

        try:
            self._api.node.update(updated_node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

    def _setup(self, args):
        # This service polls for patchset nodes instead of listening to
        # pub/sub events, as it also needs to wait for the parent checkout
        # node to complete before processing
        return None

    def _has_allowed_domain(self, url):
        domain = urlparse(url).hostname
        if domain not in self._service_config.allowed_domains:
            raise RuntimeError(
                f"Forbidden patch domain {domain}, allowed domains: "
                f"{self._service_config.allowed_domains}"
            )

    def _get_patch_artifacts(self, patchset_node):
        node_artifacts = patchset_node.get("artifacts")
        if not node_artifacts:
            raise ValueError(
                f"Patchset node {patchset_node['id']} has no artifacts"
            )

        for patch_mbox_url in node_artifacts.values():
            self._has_allowed_domain(patch_mbox_url)

        # Sort patches numerically (patch0, patch1, ..., patch10)
        def patch_key(item):
            match = re.search(r"(\d+)$", item[0])
            return int(match.group(1)) if match else 0

        return dict(sorted(node_artifacts.items(), key=patch_key))

    def _gen_checkout_name(self, checkout_node):
        # The directory inside the tarball matches the tarball file name,
        # so derive it from the artifact URL to stay consistent with the
        # naming used by the tarball service
        tarball_url = checkout_node["artifacts"]["tarball"]
        tar_filename = os.path.basename(urlparse(tarball_url).path)
        return tar_filename.removesuffix(".tar.gz")

    def _process_patchset(self, checkout_node, patchset_node):
        if not checkout_node.get("artifacts", {}).get("tarball"):
            raise ValueError(
                f"Checkout node {checkout_node['id']} has no tarball artifact"
            )

        patch_artifacts = self._get_patch_artifacts(patchset_node)

        # Tarball download implicitely removes destination dir
        # there's no need to cleanup this directory
        self._download_checkout_archive(
            download_path=self._service_config.kdir,
            tarball_url=checkout_node["artifacts"]["tarball"],
        )

        checkout_name = self._gen_checkout_name(checkout_node)
        checkout_path = os.path.join(self._service_config.kdir, checkout_name)

        patchset_hash = self._apply_patches(checkout_path, patch_artifacts)
        patchset_hash_short = patchset_hash[
            : self._service_config.patchset_short_hash_len
        ]

        tarball_path = self._make_tarball(
            target_dir=checkout_path,
            tarball_name=f"{checkout_name}-{patchset_hash_short}",
        )
        tarball_url = self._push_tarball(tarball_path)

        self._update_node(
            patchset_node=patchset_node,
            checkout_node=checkout_node,
            tarball_url=tarball_url,
            patchset_hash=patchset_hash,
        )

    def _mark_failed(self, patchset_node):
        node = patchset_node.copy()
        node.update(
            {
                "state": "done",
                "result": "fail",
            }
        )
        try:
            self._api.node.update(node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

    def _mark_failed_if_no_parent(self, patchset_node):
        if not patchset_node["parent"]:
            self.log.error(
                f"Patchset node {patchset_node['id']} has no parent "
                "checkout node, marking node as failed"
            )
            self._mark_failed(patchset_node)
            return True

        return False

    def _mark_failed_if_parent_failed(self, patchset_node, checkout_node):
        if (
            checkout_node["state"] == "done"
            and checkout_node["result"] == "fail"
        ):
            self.log.error(
                f"Parent checkout node {checkout_node['id']} failed, "
                f"marking patchset node {patchset_node['id']} as failed"
            )
            self._mark_failed(patchset_node)
            return True

        return False

    def _process_pending_patchset_nodes(self):
        patchset_nodes = self._api.node.find(
            {
                "name": "patchset",
                "state": "running",
            }
        )

        if patchset_nodes:
            self.log.debug(f"Found patchset nodes: {patchset_nodes}")

        for patchset_node in patchset_nodes:
            if self._mark_failed_if_no_parent(patchset_node):
                continue

            checkout_node = self._api.node.get(patchset_node["parent"])

            if self._mark_failed_if_parent_failed(patchset_node, checkout_node):
                continue

            if checkout_node["state"] == "running":
                self.log.info(
                    f"Patchset node {patchset_node['id']} is waiting "
                    f"for checkout node {checkout_node['id']} to complete"
                )
                continue

            try:
                self.log.info(
                    f"Processing patchset node: {patchset_node['id']}"
                )
                self._process_patchset(checkout_node, patchset_node)
            except Exception as e:
                self.log.error(
                    f"Patchset node {patchset_node['id']} "
                    f"processing failed: {e}"
                )
                self.log.traceback()
                self._mark_failed(patchset_node)

    def _run(self, _sub_id):
        self.log.info("Polling for patchset nodes")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            try:
                self._process_pending_patchset_nodes()
            except requests.exceptions.RequestException as e:
                self.log.error(f"Error polling patchset nodes: {e}")

            time.sleep(self._service_config.polling_delay_secs)


class cmd_run(Command):
    help = (
        "Wait for a checkout node to be available "
        "and push a source+patchset tarball"
    )
    args = [
        Args.kdir,
        Args.output,
        Args.api_config,
        Args.storage_config,
    ]
    opt_args = [
        Args.verbose,
        Args.storage_cred,
        {
            "name": "--name",
            "help": "Name of pipeline instance",
        },
    ]

    def __call__(self, configs, args):
        return Patchset(configs, args, "patchset").run(args)


if __name__ == "__main__":
    opts = parse_opts("patchset", globals())
    yaml_configs = opts.get_yaml_configs() or "config"
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
