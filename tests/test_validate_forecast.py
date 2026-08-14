#!/usr/bin/env python3

import unittest

from validate_forecast import _validate_job_descendants


class TestValidateJobDescendants(unittest.TestCase):
    @staticmethod
    def _report():
        return {
            "owner": "kernelci",
            "findings": [],
            "external": [],
            "non_scheduler": [],
        }

    def test_reports_missing_grandchild(self):
        report = self._report()
        root = {"id": "job-1", "name": "blktests", "result": "pass"}
        child = {
            "id": "job-2",
            "parent": "job-1",
            "name": "blktests-ddp-x86",
            "result": "pass",
            "data": {"platform": "x86"},
        }
        edges = {
            "blktests": {("blktests-ddp-x86", "x86", "job", "lava")},
            "blktests-ddp-x86": {("nipa-update", None, "job", "shell")},
        }

        _validate_job_descendants(
            report,
            {
                ("blktests", "blktests-ddp-x86"),
                ("blktests-ddp-x86", "nipa-update"),
            },
            edges,
            {"job-1": [child]},
            [root],
        )

        self.assertEqual(
            report["findings"],
            [
                {
                    "category": "MISSING_JOB",
                    "parent": "job-2",
                    "parent_name": "blktests-ddp-x86",
                    "job": "nipa-update",
                    "platform": None,
                    "runtime": "shell",
                }
            ],
        )

    def test_failed_job_suppresses_missing_descendants(self):
        report = self._report()
        root = {
            "id": "job-1",
            "name": "blktests-ddp-x86",
            "result": "fail",
        }
        edges = {"blktests-ddp-x86": {("nipa-update", None, "job", "shell")}}

        _validate_job_descendants(
            report,
            {("blktests-ddp-x86", "nipa-update")},
            edges,
            {},
            [root],
        )

        self.assertEqual(report["findings"], [])


if __name__ == "__main__":
    unittest.main()
