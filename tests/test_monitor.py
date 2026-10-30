#!/usr/bin/env python3

import unittest

from kernelci.api.latest import NodeStates
from src.monitor import STATE_MAP


class TestMonitorStateMap(unittest.TestCase):
    def test_state_map_covers_all_node_states(self):
        api_states = {state.value for state in NodeStates}
        self.assertLessEqual(api_states, set(STATE_MAP))

    def test_prepared_has_display_name(self):
        self.assertEqual(STATE_MAP["prepared"], "Prepared")


if __name__ == "__main__":
    unittest.main()
