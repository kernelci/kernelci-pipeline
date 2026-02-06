#!/usr/bin/env python3

import unittest
import types
from unittest.mock import MagicMock
from src.scheduler import Scheduler


class TestSchedulerTreePriority(unittest.TestCase):
    """Test tree priority lookup in scheduler"""

    def test_get_tree_priority_found(self):
        """Test _get_tree_priority returns correct priority when tree/branch match"""
        scheduler = MagicMock(spec=Scheduler)

        tree_mock = MagicMock()
        tree_mock.name = 'mainline'

        build_config_mock = MagicMock()
        build_config_mock.tree = tree_mock
        build_config_mock.branch = 'master'
        build_config_mock.priority = 'high'

        scheduler._build_configs = {'mainline': build_config_mock}

        result = Scheduler._get_tree_priority(scheduler, 'mainline', 'master')
        self.assertEqual(result, 'high')

    def test_get_tree_priority_not_found(self):
        """Test _get_tree_priority returns None when tree/branch not found"""
        scheduler = MagicMock(spec=Scheduler)

        tree_mock = MagicMock()
        tree_mock.name = 'mainline'

        build_config_mock = MagicMock()
        build_config_mock.tree = tree_mock
        build_config_mock.branch = 'master'
        build_config_mock.priority = 'high'

        scheduler._build_configs = {'mainline': build_config_mock}

        result = Scheduler._get_tree_priority(scheduler, 'nonexistent', 'branch')
        self.assertIsNone(result)

    def test_get_tree_priority_none_value(self):
        """Test _get_tree_priority returns None when priority is None"""
        scheduler = MagicMock(spec=Scheduler)

        tree_mock = MagicMock()
        tree_mock.name = 'stable'

        build_config_mock = MagicMock()
        build_config_mock.tree = tree_mock
        build_config_mock.branch = 'linux-6.6.y'
        build_config_mock.priority = None

        scheduler._build_configs = {'stable': build_config_mock}

        result = Scheduler._get_tree_priority(scheduler, 'stable', 'linux-6.6.y')
        self.assertIsNone(result)


class TestSchedulerQueueDepth(unittest.TestCase):
    """Test queue-depth throttling logic in scheduler."""

    def _make_common_mocks(self):
        scheduler = MagicMock(spec=Scheduler)
        scheduler.log = MagicMock()

        runtime = MagicMock()
        runtime.config.lab_type = 'lava'
        runtime.config.name = 'lab-test'

        job_config = MagicMock()
        job_config.name = 'baseline'
        job_config.params = {'device_type': 'beaglebone-black'}

        platform = MagicMock()
        platform.name = 'beaglebone-black'

        return scheduler, runtime, job_config, platform

    def test_queue_depth_uses_scaled_limit_per_device(self):
        """Scaled limit should be per-device depth * online device count."""
        scheduler, runtime, job_config, platform = self._make_common_mocks()
        runtime.config.max_queue_depth = 10
        runtime.get_device_names_by_type.return_value = [
            'bbb-1', 'bbb-2', 'bbb-3'
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
        runtime.get_device_names_by_type.return_value = ['bbb-1', 'bbb-2', 'bbb-3']

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

    def test_queue_depth_check_skips_when_device_query_not_available(self):
        """If device query helper is missing, queue-depth check is skipped."""
        scheduler = MagicMock(spec=Scheduler)
        scheduler.log = MagicMock()

        runtime = types.SimpleNamespace(
            config=types.SimpleNamespace(
                lab_type='lava',
                name='lab-test',
                max_queue_depth=50,
            ),
            get_devicetype_job_count=lambda _device_type: 100,
        )
        job_config = types.SimpleNamespace(
            name='baseline',
            params={'device_type': 'beaglebone-black'},
        )
        platform = types.SimpleNamespace(name='beaglebone-black')

        result = Scheduler._should_skip_due_to_queue_depth(
            scheduler, runtime, job_config, platform
        )
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
