# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

from ..parser_classes import State
from ..parser_loader import register_state
from ..errors.kbuild import find_kbuild_error

MODULE_NAME = 'kbuild'


# State functions

def detect_kbuild_start(text, start=None, end=None):
    """Processes a kernel build log output and searches for errors in
    the process.

    Parameters:
      text (str): the log or text fragment to parse

    Returns a dict containing the extracted info from the log:
      '_match_end': position in `text' where the parsing ended. If the
          parsing stopped at an error, this points to the end of the error.
      'errors': list of errors found, if any.
          See utils.kbuild_errors.find_build_error().
    """
    if start or end:
        text = text[start:end]
    data = {}
    # TODO: detection of log structure and definition of `done' (if
    # applicable)

    data['_match_end'] = end if end else len(text)
    # Check for errors
    data['errors'] = []
    error = find_kbuild_error(text)
    if error:
        data['errors'].append(error['error'])
        data['_match_end'] = error['_end']
    return data


# Create and register states

register_state(
    MODULE_NAME,
    State(
        name="Kernel build start",
        description="Initial state for a kernel build",
        function=detect_kbuild_start),
    'kbuild_start')
