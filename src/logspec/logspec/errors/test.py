# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2024 Collabora Limited
# Author: Ricardo Cañuelo <ricardo.canuelo@collabora.com>


from .linux_kernel import *


class TestError(Error):
    """Models a generic test error."""
    def __init__(self):
        super().__init__()
        self.error_type = "test"
