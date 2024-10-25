# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>


class Transition:
    """Models a transition between states.

    A transition is defined by:
      - a transition function
      - a name
      - the state it leads to

    A transition function is expected to take a dict as a parameter
    (normally, a state `data') and return True if the data matches the
    conditions to move through this transition, or False otherwise.
    """
    def __init__(self, function, name, state):
        self.function = function
        self.name = name
        self.state = state

    def __str__(self):
        return f"<{self.name}> into <{self.state.name}>\n"


class State:
    """Implements a state in a FSM.

    A State contains:
      - a name and description
      - (optional) a function that runs when the state is entered
      - (optional) a list of transitions
      - (optional) state-specific data
    """
    def __init__(self, name, description=None, transitions=None, function=None):
        self.name = name
        self.description = description
        self.function = function
        self.transitions = transitions
        self.data = {}

    def run(self, *params):
        """Runs the state function, if defined.

        Normally, a State function will do a number of state-specific
        operations and checks, and update the state `data' with the
        results. In a typical scenario, the State function will end up
        calculating some kind of `done' condition and saving that
        information in its `data' for a transition function to check
        later.

        Parameters:
          - params (any): parameters to be passed to the State function

        Returns:
          The return value of the State function, or None if the State
          doesn't have a function defined
        """
        if self.function:
            self.data = self.function(*params)
            return self.data
        return None

    def transition(self):
        """Checks the State transitions, if defined. For every
        transition in the State, it checks if the transition function
        triggers or not, and then returns the state of the first triggered
        transition.

        Returns:
          The target state of the first triggered transition found, or
          None if no transition triggered or if the State doesn't have
          any outgoing transitions.
        """
        if not self.transitions:
            return None
        for t in self.transitions:
            if t.function(self.data):
                return t.state
        return None

    def __str__(self):
        string = (f"State <{self.name}>: {self.description}\n"
                  f"  function: {self.function}\n")
        if self.transitions:
            string += "  transitions:\n"
            for t in self.transitions:
                string += f"  - {t}\n"
        return string
