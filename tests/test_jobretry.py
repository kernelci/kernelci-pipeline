#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

import asyncio
import contextlib
import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)

from kernelci.scheduler import Scheduler  # noqa: E402

import lava_callback  # noqa: E402

KBUILD = {
    "id": "kbuild-node",
    "kind": "kbuild",
    "name": "kbuild-gcc-14-arm",
    "path": ["checkout", "kbuild-gcc-14-arm"],
    "parent": "checkout-node",
    "state": "done",
    "result": "pass",
    "retry_counter": 0,
    "submitter": "service:pipeline",
    "owner": "staging.kernelci.org",
    "data": {
        "kernel_revision": {"tree": "mainline", "branch": "master"},
        "artifacts": {"kernel": "https://example.org/zImage"},
    },
}

JOB = {
    "id": "job-node",
    "kind": "job",
    "name": "baseline-arm",
    "parent": KBUILD["id"],
    "state": "done",
    "result": "incomplete",
    "retry_counter": 3,
    "data": {"platform": "qemu-arm"},
}


class TestJobRetry(unittest.TestCase):
    def retry(self, jobfilter=None):
        nodes = {KBUILD["id"]: KBUILD, JOB["id"]: JOB}
        helper = mock.Mock()
        helper.api.node.get.side_effect = lambda node_id: copy.deepcopy(
            nodes.get(node_id)
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    lava_callback,
                    "validate_permissions",
                    return_value={"email": "dev@example.org"},
                )
            )
            stack.enter_context(
                mock.patch.object(
                    lava_callback, "_get_api_helper", return_value=helper
                )
            )
            stack.enter_context(
                mock.patch.dict(
                    lava_callback.SETTINGS,
                    {"DEFAULT": {"api_config": "staging"}},
                )
            )
            response = asyncio.run(
                lava_callback.jobretry(
                    lava_callback.JobRetry(
                        nodeid=JOB["id"], jobfilter=jobfilter
                    ),
                    mock.Mock(),
                    "token",
                )
            )
        self.assertEqual(response.status_code, 200)
        helper.api.send_event.assert_called_once()
        channel, event = helper.api.send_event.call_args.args
        return channel, event["data"]

    def test_event_matches_kbuild_schedule(self):
        _, event = self.retry()
        scheduler = Scheduler(lava_callback.CONFIGS, {})
        matches = [
            entry
            for entry in scheduler.get_configs(event)
            if entry.job == JOB["name"]
            and JOB["data"]["platform"] in entry.platforms
        ]
        self.assertTrue(matches)

    def test_event_published_on_retry_channel(self):
        channel, _ = self.retry()
        self.assertEqual(channel, "retry")

    def test_event_limited_to_original_platform(self):
        _, event = self.retry()
        self.assertEqual(event["platform_filter"], ["qemu-arm"])

    def test_event_keeps_requested_jobs(self):
        _, event = self.retry(jobfilter=["baseline-arm-child+"])
        self.assertIn(JOB["name"], event["jobfilter"])
        self.assertIn("baseline-arm-child+", event["jobfilter"])

    def test_each_request_gets_its_own_id(self):
        _, first = self.retry()
        _, second = self.retry()
        self.assertTrue(first["retry_request_id"])
        self.assertNotEqual(
            first["retry_request_id"], second["retry_request_id"]
        )

    def test_event_records_retried_job(self):
        _, event = self.retry()
        self.assertEqual(event["debug"]["retry_by"], JOB["id"])


if __name__ == "__main__":
    unittest.main()
