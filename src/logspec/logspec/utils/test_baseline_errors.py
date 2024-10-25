# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

import re

from .defs import *
from ..errors.test import *


def find_test_baseline_dmesg_error(text):
    end = 0
    match = re.search(r'kern  :(?P<message>.*)', text)
    if not match:
        return None
    error = TestError()
    error.error_type += ".baseline.dmesg"
    error.error_summary = match.group('message')
    return {
        'error': error,
        '_end': match.end(),
    }
