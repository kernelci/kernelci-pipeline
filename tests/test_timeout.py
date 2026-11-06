#!/usr/bin/env python3

import types
import unittest
from unittest.mock import MagicMock, patch

from kernelci.api.latest import NodeStates
from src.timeout import TimeoutService


def _make_service():
    args = types.SimpleNamespace(name=None, api_config="test")
    configs = {"api": {"test": MagicMock()}}
    api = MagicMock()
    api.node.states = NodeStates
    api.user.whoami.return_value = {"username": "test-user"}

    with patch("base.Logger", MagicMock()), patch(
        "kernelci.api.get_api", return_value=api
    ), patch("base.APIHelper", MagicMock()):
        service = TimeoutService(configs, args, "timeout")
    return service, api


class TestTimeoutPendingStates(unittest.TestCase):
    def test_prepared_is_treated_as_pending(self):
        service, _ = _make_service()
        self.assertIn("prepared", service._pending_states)

    def test_timeout_polls_prepared_nodes(self):
        service, api = _make_service()

        def find(filters):
            if filters.get("state") == "prepared":
                return [
                    {"id": "job", "state": "prepared", "owner": "test-user"}
                ]
            return []

        api.node.find.side_effect = find

        nodes = service._get_pending_nodes({"timeout__lt": "2026-01-01"})

        self.assertIn("job", nodes)

    def test_prepared_child_keeps_parent_open(self):
        service, api = _make_service()
        api.node.find.return_value = [{"id": "child", "state": "prepared"}]

        count = service._count_running_child_nodes("parent")

        self.assertEqual(count, 1)

    def test_mixed_legacy_and_new_nodes_are_both_pending(self):
        service, _ = _make_service()
        self.assertIn("available", service._pending_states)
        self.assertIn("prepared", service._pending_states)

    def test_mixed_legacy_and_new_nodes_are_polled(self):
        service, api = _make_service()
        nodes_by_state = {
            "available": [
                {"id": "legacy", "state": "available", "owner": "test-user"}
            ],
            "prepared": [
                {"id": "new", "state": "prepared", "owner": "test-user"}
            ],
        }

        def find(filters):
            return nodes_by_state.get(filters.get("state"), [])

        api.node.find.side_effect = find
        pending = service._get_pending_nodes({"timeout__lt": "2026-01-01"})
        self.assertIn("legacy", pending)
        self.assertIn("new", pending)
