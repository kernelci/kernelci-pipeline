#!/usr/bin/env python3

import unittest
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


if __name__ == '__main__':
    unittest.main()
