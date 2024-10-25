# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

"""This module manages the loading and creation of a parser. The components
of a parser (state classes and transition functions) are defined in
separate modules in the 'states' and 'transition_functions'
respectively, and these modules are dynamically imported by
`parser_loader()' when a parser is created from a definition.

These modules are expected to call the `register_state()' and
`register_transition_function()' functions in this module when
imported. All the registered states and transition functions are kept by
this module.
"""

import importlib
import os
from .parser_classes import Transition
from .version import __version__


# Dicts to hold the loaded states and transition functions registered by
# external modules
states = {}
transition_functions = {}

STATE_MOD_PREFIX = 'logspec.logspec.states'
# TRANSFUNC_MOD_PREFIX = 'logspec.transition_functions'
# STATE_MOD_PREFIX = 'logspec.states'
TRANSFUNC_MOD_PREFIX = 'logspec.logspec.transition_functions'


def register_state(module, state, name):
    """Registers a State class.

    Parameters:
      module: name of the module that registers the state
      state: State class to register
      name: name of the state

    Notes:
      Will raise a RuntimeError if the state is already registered
    """
    full_name = f"{module}.{name}"
    if full_name in states:
        raise RuntimeError(f"State <{full_name}> already registered")
    states[full_name] = state


def register_transition_function(module, function, name):
    """Registers a transition function.

    Parameters:
      module: name of the module that registers the function
      function: function to register
      name: name of the function

    Notes:
      Will raise a RuntimeError if the function is already registered
    """
    full_name = f"{module}.{name}"
    if full_name in transition_functions:
        raise RuntimeError(f"Transition function <{full_name}> already registered")
    transition_functions[full_name] = function


def parser_loader(parser_defs, name):
    """Reads a parser definition, loads the required modules and creates
    the parser.

    Parameters:
      parser_defs: a dict of parser definitions, typically loaded from a yaml
          file
      name: the name of the parser definition to load, which must exist in
          parser_defs

    Returns:
      The start state of the parser.

    Notes:
      Will raise ModuleNotFoundError if any of the specified modules
      fail to load and RuntimeError if there's any problem creating the
      parser.
    """
    if 'version' in parser_defs:
        _, current_parser_classes_version, _ = __version__.split('.')
        _, parser_defs_version, _ = parser_defs['version'].split('.')
        if int(current_parser_classes_version) != int(parser_defs_version):
            raise RuntimeError(f"Parser definitions version {parser_defs['version']} may "
                               f"not be supported by logspec version {__version__}.")
    if name not in parser_defs['parsers']:
        raise RuntimeError(f"Definition of parser {name} not found.")
    parser = parser_defs['parsers'][name]
    # Load state modules
    for state_def in parser['states']:
        module, _ = os.path.splitext(state_def['name'])
        try:
            importlib.import_module(f'{STATE_MOD_PREFIX}.{module}')
        except ModuleNotFoundError:
            msg = (f"Module states.{module} not found. "
                   f"Error loading state {state_def['name']}")
            raise ModuleNotFoundError(msg) from None
    # Load transition function modules and build the parser
    for state_def in parser['states']:
        if not state_def['name'] in states:
            raise RuntimeError(f"State {state_def['name']} not found.")
        states[state_def['name']].transitions = []
        if 'transitions' in state_def:
            for transition_def in state_def['transitions']:
                module, _ = os.path.splitext(transition_def['function'])
                try:
                    importlib.import_module(f'{TRANSFUNC_MOD_PREFIX}.{module}')
                except ModuleNotFoundError:
                    msg = (f"Module transition_functions.{module} not found. "
                           f"Error loading transition function {transition_def['function']}")
                    raise ModuleNotFoundError(msg) from None
                try:
                    function = transition_functions[transition_def['function']]
                    state = states[transition_def['state']]
                except KeyError as err:
                    msg = (f"Error loading transition function "
                           f"{transition_def['function']}. {str(err)} not found.")
                    raise RuntimeError(msg) from None
                states[state_def['name']].transitions.append(
                    Transition(function, transition_def['function'], state))
    if parser['start_state'] not in states:
        raise RuntimeError(f"Start state {parser['start_state']} not found.")
    return states[parser['start_state']]
