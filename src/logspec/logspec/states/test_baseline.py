# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

import re
from ..parser_classes import State
from ..utils.test_baseline_errors import find_test_baseline_dmesg_error
from ..parser_loader import register_state

MODULE_NAME = 'test_baseline'


# State functions

def detect_test_baseline(text, start=None, end=None):
    start_tags = [
        '/opt/kernelci/dmesg.sh',
    ]
    if start or end:
        text = text[start:end]
    data = {}
    regex = '|'.join(start_tags)

    # Check for test start
    match = re.search(regex, text)
    if not match:
        data['test.baseline.start'] = False
        data['_match_end'] = end if end else len(text)
        return data
    test_start = match.end()
    test_end = None
    data['test.baseline.start'] = True

    # Check for test end
    end_tags = [
        # NOTE: LAVA-specific
        '<LAVA_TEST_RUNNER EXIT>',
    ]
    match = re.search(regex, text[test_start:])
    if match:
        test_end = test_start + match.end()
        data['_match_end'] = test_end + start if start else test_end
    else:
        data['_match_end'] = end if end else len(text)

    # Check for errors during the test run. If the test end was
    # detected, search between the test beginning and end. Otherwise
    # search in the full log.
    data['errors'] = []
    while True:
        error = find_test_baseline_dmesg_error(text[test_start:test_end])
        if not error:
            break
        data['errors'].append(error['error'])
        test_start += error['_end']
    return data


# Create and register states

register_state(
    MODULE_NAME,
    State(
        name="Baseline test",
        description="Search and process a baseline test",
        function=detect_test_baseline),
    'test_baseline')
