# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

import re

from .defs import *
from ..errors.linux_kernel import *


def find_error_report(text, include_generic=True):
    """Finds a kernel error report in a text log.

    Current types of error reports supported:
      - Generic "cut here" blocks
      - NULL pointer dereferences

    Parameters:
      text (str): the log or text fragment to parse

    Returns:
    If an error report was found, it returns a dict containing:
      'error': specific error object containing the structured error
          info, or None if the parser failed to parse the error block
          completely.
      'end': position in the text right after the parsed block
    None if no error report was found.

    """
    # Tags to look for. For every tag found, the parsing is delegated to
    # the appropriate object.
    # Key: tag name, value: error class (in logspec/errors/linux_kernel.py)
    tags = {
        'null_pointer': NullPointerDereference,
        'bug': KernelBug,
        'ubsan': UBSANError,
        'kernel_panic': KernelPanic,
        # 'Oops': {
        #     'regex': f'{LINUX_TIMESTAMP} Oops:',
        #     'error_class': KernelOops,
        # },
    }
    generic_tags = {
        'generic': GenericError,
    }
    if include_generic:
        tags.update(generic_tags)

    regex = '|'.join([f"(?P<{tag}>{error_class.start_marker_regex})" for tag, error_class in tags.items()])
    match = re.search(regex, text)
    if match:
        # Detect which of the tags was found and dispatch the parsing to
        # the right function
        matched_tag = [tag for tag, value in match.groupdict().items() if value is not None][0]
        if not matched_tag:
            return None
        if matched_tag == 'generic':
            # Check if a more specific error can be found inside a
            # "cut here" block and parse it
            end_match = re.search(tags[matched_tag].end_marker_regex, text[match.end():])
            if end_match:
                start_pos = match.end()
                end_pos = start_pos + end_match.end()
                report = find_error_report(text[start_pos:end_pos], include_generic=False)
                if report:
                    report['_end'] = end_pos
                    return report
        # Base case: parse error
        error = tags[matched_tag]()
        error_parse_end = error.parse(text[match.start():])
        # Skip the error if it failed to parse
        if not error_parse_end:
            return {
                'error': None,
                '_end': match.end()
            }
        end = match.start() + error_parse_end
        return {
            'error': error,
            '_end': end,
        }
    return None


def find_kernel_error(text):
    """Find kernel errors in a text segment.

    Currently supported:
      - kernel error reports (find_error_report)

    Parameters:
      text (str): the log or text fragment to parse

    Returns:
    If an error report was found, it returns a dict containing:
      'error': specific error object containing the structured error
          info. None if no error couldn't be properly parsed
      'end': position in the text right after the parsed block
    None if no error report was found.
    """
    report = find_error_report(text)
    return report
