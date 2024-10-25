# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

import json
import logging
import yaml
from . import version, default_parser_defs_file
from .parser_loader import parser_loader
from .utils.defs import JsonSerialize, JsonSerializeDebug


def format_data_output(data, full=False):
    """Returns a string containing the JSON-serialized version of
    `data'. By default, all fields starting with '_' are not printed,
    unless `full' is set to True.
    """
    def remove_keys(data_dict, prefix):
        for key in list(data_dict):
            if key.startswith(prefix):
                del data_dict[key]
        # Remove recursively on any remaining nested dict (even inside
        # lists)
        for key in list(data_dict):
            if isinstance(data_dict[key], dict):
                remove_keys(data_dict[key], prefix)
            if isinstance(data_dict[key], list):
                for d in data_dict[key]:
                    if isinstance(d, dict):
                        remove_keys(d, prefix)
    if full:
        json_serializer = JsonSerializeDebug
    else:
        json_serializer = JsonSerialize
        remove_keys(data, '_')
    return json.dumps(data, indent=4, sort_keys=True, cls=json_serializer, ensure_ascii=False)


def parse_log(log, start_state):
    """Parses a log (str) using a loaded FSM that starts in
    `start_state'.

    Returns:
      The FSM data (dict) after the parsing is done.
    """
    state = start_state
    data = {}
    cumulative_errors = []
    log_start = 0
    while state:
        # The log fragment to parse is adjusted after every state
        # transition if the state function sets a `match_end' field in
        # its data. This is supposed to mark the position where the
        # parsing ended, so the next state will start parsing from
        # there.
        #
        # TODO: If some states need to do non-sequential parsing we can
        # explicitly pass the `start' and `end' parsing positions
        # together with the full log to `run()' instead of passing a
        # narrowed down log.
        logging.debug(f"State: {state}")
        state_data = state.run(log)
        state = state.transition()
        if 'errors' in state_data:
            cumulative_errors.extend(state_data['errors'])
        data.update(state_data)
        if '_match_end' in data:
            log_start += data['_match_end']
            log = log[data['_match_end']:]
            data['_match_end'] = log_start
    data['errors'] = cumulative_errors
    return data


def parse_log_file(log_file_path, start_state):
    """Parses a log file using a loaded FSM that starts in
    `start_state'.

    Returns:
      The FSM data (dict) after the parsing is done.
    """
    with open(log_file_path, 'r') as log_file:
        log = log_file.read()
    return parse_log(log, start_state)


def load_parser(parser_id, parser_defs_file=default_parser_defs_file):
    """Reads a parser definition file and loads and initializes the parser
    specified by `parser_id'.

    Returns:
      The start state of the loaded parser.
    """
    with open(parser_defs_file, 'r') as parser_file:
        parser_defs = yaml.safe_load(parser_file)
        assert parser_defs, f"Error loading parser definitions: {parser_defs_file}"
        start_state = parser_loader(parser_defs, parser_id)
        assert start_state, f"Error loading parser {parser_id}"
        return start_state


def load_parser_and_parse_log(log_file_path, parser_id, parser_defs_file=None):
    """Reads a parser definition file, loads and initializes the parser
    specified by `parser_id' and uses it to parse a log file.

    Returns:
      The parser data (dict) after the parsing is done.
    """
    start_state = load_parser(parser_id, parser_defs_file)
    return parse_log_file(log_file_path, start_state)


def logspec_version():
    return version.__version__
