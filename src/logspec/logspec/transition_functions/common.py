# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>

from ..parser_loader import register_transition_function

MODULE_NAME = 'common'

# def jump_to_state(_):
#     return True

register_transition_function(
    MODULE_NAME,
    lambda x: True,
    'jump_to_state')
