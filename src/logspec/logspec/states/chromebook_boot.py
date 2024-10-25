# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

import re
from ..parser_classes import State
from ..parser_loader import register_state
from ..utils.defs import *

MODULE_NAME = 'chromebook_boot'


# Helper functions

def parse_bootloader_errors(text):
    data = {}
    return data


# State functions

def detect_bootloader_start(text, start=None, end=None):
    """Detects the start of a Chromebook bootloader run in a boot log.

    Parameters:
      text (str): the log or text fragment to parse

    Returns a dict containing the extracted info from the log:
      'bootloader.start': True if the bootloader was detected, False
          otherwise
      'bootloader.id': name or tag that identifies the bootloader found
      '_match_end': position in `text' where the parsing ended
    """
    # Patterns (tags) to search for. The regexp will be formed by or'ing
    # them
    tags = [
        "Starting depthcharge",
    ]
    if start or end:
        text = text[start:end]
    data = {}
    regex = '|'.join(tags)
    match = re.search(regex, text)
    if match:
        data['_match_end'] = match.end()
        data['bootloader.start'] = True
        data['bootloader.id'] = 'depthcharge'
    else:
        data['_match_end'] = end if end else len(text)
        data['bootloader.start'] = False
    return data


def detect_bootloader_end(text, start=None, end=None):
    """Detects the end of a successful Chromebook bootloader execution
    in a text log and searches for errors during the process.

    Parameters:
      text (str): the log or text fragment to parse

    Returns a dict containing the extracted info from the log:
      'bootloader.done': True if the bootloader was detected to boot
          successfuly, False otherwise
      '_match_end': position in `text' where the parsing ended
    """
    # Patterns (tags) to search for. The regexp will be formed by or'ing
    # them
    tags = [
        "Starting kernel ...",
        "jumping to kernel",
        f"{LINUX_TIMESTAMP} Booting Linux",
    ]
    if start or end:
        text = text[start:end]
    data = {}
    regex = '|'.join(tags)
    match = re.search(regex, text)
    if match:
        data['_match_end'] = match.end() + start if start else match.end()
        data['bootloader.done'] = True
        # Search for errors up until the found tag
        data.update(parse_bootloader_errors(text[:match.start()]))
    else:
        data['_match_end'] = end if end else len(text)
        data['bootloader.done'] = False
    return data


# Create and register states

register_state(
    MODULE_NAME,
    State(
        name="Chromebook boot",
        description="Initial state for a Chromebook that was powered on",
        function=detect_bootloader_start),
    'chromebook_boot')

register_state(
    MODULE_NAME,
    State(
        name="Chromebook bootloader started",
        description="Chromebook bootloader found",
        function=detect_bootloader_end),
    'chromebook_bootloader_started')

register_state(
    MODULE_NAME,
    State(
        name="Chromebook boot (second stage)",
        description="Initial state for a Chromebook that was powered on (second stage)",
        function=detect_bootloader_start),
    'chromebook_boot_stage2')

register_state(
    MODULE_NAME,
    State(
        name="Chromebook bootloader (second stage) started",
        description="Chromebook bootloader (second stage) found",
        function=detect_bootloader_end),
    'chromebook_bootloader_stage2_started')
