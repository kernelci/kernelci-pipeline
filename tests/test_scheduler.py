#!/usr/bin/env python3

import collections
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

from src.scheduler import Scheduler


class TestSchedulerTreePriority(unittest.TestCase):
    """Test tree priority lookup in scheduler"""

    def test_get_tree_priority_found(self):
        """Test _get_tree_priority returns correct priority when tree/branch match"""
        scheduler = MagicMock(spec=Scheduler)

        tree_mock = MagicMock()
        tree_mock.name = "mainline"

        build_config_mock = MagicMock()
        build_config_mock.tree = tree_mock
        build_config_mock.branch = "master"
        build_config_mock.priority = "high"

        scheduler._build_configs = {"mainline": build_config_mock}

        result = Scheduler._get_tree_priority(scheduler, "mainline", "master")
        self.assertEqual(result, "high")

    def test_get_tree_priority_not_found(self):
        """Test _get_tree_priority returns None when tree/branch not found"""
        scheduler = MagicMock(spec=Scheduler)

        tree_mock = MagicMock()
        tree_mock.name = "mainline"

        build_config_mock = MagicMock()
        build_config_mock.tree = tree_mock
        build_config_mock.branch = "master"
        build_config_mock.priority = "high"

        scheduler._build_configs = {"mainline": build_config_mock}

        result = Scheduler._get_tree_priority(
            scheduler, "nonexistent", "branch"
        )
        self.assertIsNone(result)

    def test_get_tree_priority_none_value(self):
        """Test _get_tree_priority returns None when priority is None"""
        scheduler = MagicMock(spec=Scheduler)

        tree_mock = MagicMock()
        tree_mock.name = "stable"

        build_config_mock = MagicMock()
        build_config_mock.tree = tree_mock
        build_config_mock.branch = "linux-6.6.y"
        build_config_mock.priority = None

        scheduler._build_configs = {"stable": build_config_mock}

        result = Scheduler._get_tree_priority(
            scheduler, "stable", "linux-6.6.y"
        )
        self.assertIsNone(result)


class TestSchedulerQueueDepth(unittest.TestCase):
    """Test queue-depth throttling logic in scheduler."""

    def _make_common_mocks(self):
        scheduler = MagicMock(spec=Scheduler)
        scheduler.log = MagicMock()
        scheduler._telemetry = MagicMock()
        # Neutral priority factor by default; ceiling-math tests override it.
        scheduler._queue_priority_factor.return_value = 1.0

        runtime = MagicMock()
        runtime.config.lab_type = "lava"
        runtime.config.name = "lab-test"
        runtime.config.disable_queue_limit = False
        # Disable priority weighting by default so these tests exercise the
        # plain per-device ceiling; priority scaling is covered separately.
        runtime.config.max_queue_depth_priority_factor = 1.0

        job_config = MagicMock()
        job_config.name = "baseline"
        job_config.params = {"device_type": "beaglebone-black"}

        platform = MagicMock()
        platform.name = "beaglebone-black"

        return scheduler, runtime, job_config, platform

    def test_queue_depth_uses_scaled_limit_per_device(self):
        """Scaled limit should be per-device depth * online device count."""
        scheduler, runtime, job_config, platform = self._make_common_mocks()
        runtime.config.max_queue_depth = 10
        runtime.get_device_names_by_type.return_value = [
            "bbb-1",
            "bbb-2",
            "bbb-3",
        ]

        runtime.get_devicetype_job_count.return_value = 29
        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertFalse(result)

        runtime.get_devicetype_job_count.return_value = 30
        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertTrue(result)

    def test_queue_depth_scales_with_max_queue_depth(self):
        """max_queue_depth is treated as per-device queue budget."""
        scheduler, runtime, job_config, platform = self._make_common_mocks()
        runtime.config.max_queue_depth = 50
        runtime.get_device_names_by_type.return_value = [
            "bbb-1",
            "bbb-2",
            "bbb-3",
        ]

        runtime.get_devicetype_job_count.return_value = 149
        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertFalse(result)

        runtime.get_devicetype_job_count.return_value = 150
        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertTrue(result)

    def test_queue_depth_skipped_when_disable_queue_limit(self):
        """When disable_queue_limit is True, queue depth check is bypassed."""
        scheduler, runtime, job_config, platform = self._make_common_mocks()
        runtime.config.max_queue_depth = 10
        runtime.config.disable_queue_limit = True
        runtime.get_device_names_by_type.return_value = ["bbb-1"]
        runtime.get_devicetype_job_count.return_value = 9999

        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertFalse(result)
        runtime.get_devicetype_job_count.assert_not_called()

    def test_queue_depth_ceiling_scales_with_priority_factor(self):
        """A high-priority job gets a larger but still bounded ceiling."""
        scheduler, runtime, job_config, platform = self._make_common_mocks()
        runtime.config.max_queue_depth = 50
        runtime.get_device_names_by_type.return_value = ["bbb-1", "bbb-2"]
        # Highest priority -> factor == max (2x): ceiling 50 * 2 * 2 = 200.
        scheduler._queue_priority_factor.return_value = 2.0

        runtime.get_devicetype_job_count.return_value = 199
        self.assertFalse(
            Scheduler._should_skip_due_to_queue_depth(
                scheduler, runtime, job_config, platform
            )
        )

        runtime.get_devicetype_job_count.return_value = 200
        self.assertTrue(
            Scheduler._should_skip_due_to_queue_depth(
                scheduler, runtime, job_config, platform
            )
        )

    def test_high_priority_job_does_not_bypass_unbounded(self):
        """Even the highest priority is hard-capped, never an open bypass."""
        scheduler, runtime, job_config, platform = self._make_common_mocks()
        runtime.config.max_queue_depth = 10
        runtime.get_device_names_by_type.return_value = ["bbb-1"]
        scheduler._queue_priority_factor.return_value = 2.0  # the cap

        # 10 * 1 device * 2x cap = 20; a deeper queue is still skipped.
        runtime.get_devicetype_job_count.return_value = 25
        self.assertTrue(
            Scheduler._should_skip_due_to_queue_depth(
                scheduler, runtime, job_config, platform
            )
        )

    def test_queue_priority_factor_scales_between_one_and_cap(self):
        """Factor goes 1.0 (lowest) -> cap (highest), linearly by fraction."""
        scheduler = MagicMock(spec=Scheduler)

        runtime = MagicMock()
        runtime.config.max_queue_depth_priority_factor = 2.0

        job_config = MagicMock()
        job_config.priority = "high"

        # Lowest priority fraction -> factor 1.0 (no boost).
        runtime.get_queue_priority_fraction.return_value = 0.0
        self.assertEqual(
            Scheduler._queue_priority_factor(
                scheduler, runtime, job_config, None
            ),
            1.0,
        )

        # Mid priority fraction -> halfway to the cap.
        runtime.get_queue_priority_fraction.return_value = 0.5
        self.assertEqual(
            Scheduler._queue_priority_factor(
                scheduler, runtime, job_config, None
            ),
            1.5,
        )

        # Highest priority fraction -> the configured cap.
        runtime.get_queue_priority_fraction.return_value = 1.0
        self.assertEqual(
            Scheduler._queue_priority_factor(
                scheduler, runtime, job_config, None
            ),
            2.0,
        )

    def test_queue_priority_factor_disabled_when_cap_is_one(self):
        """A cap of 1.0 disables weighting without querying priority."""
        scheduler = MagicMock(spec=Scheduler)

        runtime = MagicMock()
        runtime.config.max_queue_depth_priority_factor = 1.0

        job_config = MagicMock()

        result = Scheduler._queue_priority_factor(
            scheduler, runtime, job_config, None
        )
        self.assertEqual(result, 1.0)
        runtime.get_queue_priority_fraction.assert_not_called()

    def test_queue_priority_factor_uses_tree_priority_from_node(self):
        """tree_priority from the input node feeds the priority fraction."""
        scheduler = MagicMock(spec=Scheduler)
        scheduler._get_tree_priority.return_value = "high"

        runtime = MagicMock()
        runtime.config.max_queue_depth_priority_factor = 3.0
        runtime.get_queue_priority_fraction.return_value = 1.0

        job_config = MagicMock()
        job_config.priority = "low"

        input_node = {
            "data": {
                "kernel_revision": {"tree": "mainline", "branch": "master"}
            }
        }

        result = Scheduler._queue_priority_factor(
            scheduler, runtime, job_config, input_node
        )
        self.assertEqual(result, 3.0)
        scheduler._get_tree_priority.assert_called_once_with(
            "mainline", "master"
        )
        runtime.get_queue_priority_fraction.assert_called_once_with(
            "high", "low"
        )

    def test_queue_depth_check_skips_when_device_query_not_available(self):
        """If device query helper is missing, queue-depth check is skipped."""
        scheduler = MagicMock(spec=Scheduler)
        scheduler.log = MagicMock()

        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(
                lab_type="lava",
                name="lab-test",
                max_queue_depth=50,
            ),
            get_devicetype_job_count=lambda _device_type: 100,
        )
        job_config = types.SimpleNamespace(
            name="baseline",
            params={"device_type": "beaglebone-black"},
        )
        platform = types.SimpleNamespace(name="beaglebone-black")

        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertFalse(result)


class TestSchedulerDuplicateGuard(unittest.TestCase):
    """Test the process-local duplicate-job guard (kernelci-core#2912)."""

    def _make_scheduler(self):
        scheduler = MagicMock(spec=Scheduler)
        scheduler._recent_jobs = collections.OrderedDict()
        scheduler._dedup_lock = threading.Lock()
        return scheduler

    @staticmethod
    def _job_args(
        parent="parent1",
        job="kselftest-dt",
        runtime_name="lava-broonie",
        platform_name="beaglebone-black",
    ):
        input_node = {"id": parent}
        job_config = types.SimpleNamespace(name=job)
        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(name=runtime_name)
        )
        platform = types.SimpleNamespace(name=platform_name)
        return input_node, job_config, runtime, platform

    def test_identical_job_is_flagged_as_duplicate(self):
        """The first call records the job; an identical second call is a dup."""
        scheduler = self._make_scheduler()
        args = self._job_args()
        self.assertFalse(Scheduler._job_recently_scheduled(scheduler, *args, 0))
        self.assertTrue(Scheduler._job_recently_scheduled(scheduler, *args, 0))

    def test_retry_counter_is_not_suppressed(self):
        """A retry (different retry_counter) is never treated as duplicate."""
        scheduler = self._make_scheduler()
        args = self._job_args()
        self.assertFalse(Scheduler._job_recently_scheduled(scheduler, *args, 0))
        self.assertFalse(Scheduler._job_recently_scheduled(scheduler, *args, 1))

    def test_different_platform_is_not_duplicate(self):
        """Same job on a different platform is a distinct job."""
        scheduler = self._make_scheduler()
        self.assertFalse(
            Scheduler._job_recently_scheduled(
                scheduler, *self._job_args(platform_name="beaglebone-black"), 0
            )
        )
        self.assertFalse(
            Scheduler._job_recently_scheduled(
                scheduler, *self._job_args(platform_name="imx6q-udoo"), 0
            )
        )

    def test_entry_reallowed_after_ttl(self):
        """After the TTL elapses, an identical job is allowed again."""
        scheduler = self._make_scheduler()
        args = self._job_args()
        with patch("src.scheduler.time.time", return_value=1000.0):
            self.assertFalse(
                Scheduler._job_recently_scheduled(scheduler, *args, 0)
            )
        from src.scheduler import DEDUP_CACHE_TTL

        with patch(
            "src.scheduler.time.time", return_value=1000.0 + DEDUP_CACHE_TTL + 1
        ):
            self.assertFalse(
                Scheduler._job_recently_scheduled(scheduler, *args, 0)
            )

    def test_cache_size_is_bounded(self):
        """The dedup cache never grows past DEDUP_CACHE_MAX entries."""
        scheduler = self._make_scheduler()
        with patch("src.scheduler.DEDUP_CACHE_MAX", 5):
            for i in range(20):
                Scheduler._job_recently_scheduled(
                    scheduler, *self._job_args(parent=f"parent-{i}"), 0
                )
        self.assertLessEqual(len(scheduler._recent_jobs), 5)


if __name__ == "__main__":
    unittest.main()
