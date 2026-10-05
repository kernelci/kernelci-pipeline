#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Tests for tarball event routing

import copy
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from tarball import Tarball  # noqa: E402


class TestTarballEvents(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.build_config = types.SimpleNamespace(
            tree=types.SimpleNamespace(name="mainline"), branch="master"
        )
        configs = {
            "build_configs": {"mainline": self.build_config},
            "storage_configs": {"storage": object()},
        }
        args = types.SimpleNamespace(
            output=tmp.name,
            kdir=tmp.name,
            storage_config="storage",
            storage_cred=None,
        )
        with mock.patch("tarball.Service.__init__", return_value=None):
            with mock.patch("tarball.kernelci.storage.get_storage"):
                self.service = Tarball(configs, args, "tarball")
        self.service._name = "tarball"
        self.service._logger = mock.Mock()
        self.service._api = mock.Mock()
        self.service._api_helper = mock.Mock()

        # Stub Git and storage I/O; execute the real event loop, build
        # selection and node update so incorrect routing is observable.
        methods = {
            "_update_repo": False,
            "_checkout_commitid": None,
            "_get_version_from_describe": {"version": "6"},
            "_make_tarball": "/tmp/linux-base.tar.gz",
            "_push_tarball": "https://files.example/linux-base.tar.gz",
            "_get_commit_info": ([], "base commit", True),
        }
        patchers = [
            mock.patch.object(self.service, name, return_value=value)
            for name, value in methods.items()
        ]
        patchers.append(
            mock.patch(
                "tarball.kernelci.build.git_describe", return_value="v6.0"
            )
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def node(name):
        return {
            "id": name + "-id",
            "name": name,
            "kind": "checkout",
            "state": "running",
            "result": None,
            "data": {
                "kernel_revision": {
                    "tree": "mainline",
                    "branch": "master",
                    "commit": "a" * 40,
                }
            },
            "artifacts": {},
        }

    def run_events(self, *nodes):
        self.service._api_helper.receive_event_node.side_effect = [
            *((node, {}) for node in nodes),
            KeyboardInterrupt(),
        ]
        with self.assertRaises(KeyboardInterrupt):
            self.service._run("subscription")

    def test_subscribes_only_to_normal_checkout_events(self):
        self.service._setup(None)

        self.service._api_helper.subscribe_filters.assert_called_once_with(
            {
                "op": "created",
                "name": "checkout",
                "kind": "checkout",
                "state": "running",
            },
            subscriber_id="tarball:node",
        )

    def test_queued_patchset_keeps_artifacts_and_checkout_still_runs(self):
        patchset = self.node("patchset")
        patchset["artifacts"] = {
            "patch0": "https://files.example/0001.patch",
            "patch1": "https://files.example/0002.patch",
        }
        original = copy.deepcopy(patchset)
        checkout = self.node("checkout")

        self.run_events(patchset, checkout)

        self.assertEqual(patchset, original)
        self.service._update_repo.assert_called_once_with(self.build_config)
        self.service._make_tarball.assert_called_once()
        self.service._push_tarball.assert_called_once()
        self.service._api.node.update.assert_called_once()
        updated = self.service._api.node.update.call_args[0][0]
        self.assertEqual(updated["id"], checkout["id"])
        self.assertEqual(updated["state"], "available")
        self.assertEqual(updated["result"], "pass")
        self.assertEqual(
            updated["artifacts"],
            {"tarball": "https://files.example/linux-base.tar.gz"},
        )

    def test_queued_completed_patchset_is_not_overwritten(self):
        patchset = self.node("patchset")
        patchset["state"] = "available"
        patchset["result"] = "pass"
        patchset["data"]["kernel_revision"]["patchset"] = "b" * 64
        patchset["artifacts"] = {
            "patch0": "https://files.example/0001.patch",
            "patch1": "https://files.example/0002.patch",
            "tarball": "https://files.example/linux-patched.tar.gz",
        }
        original = copy.deepcopy(patchset)

        self.run_events(patchset)

        self.assertEqual(patchset, original)
        self.service._update_repo.assert_not_called()
        self.service._make_tarball.assert_not_called()
        self.service._push_tarball.assert_not_called()
        self.service._api.node.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
