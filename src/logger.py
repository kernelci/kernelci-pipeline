#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

"""Logger module"""

import logging
import logging.config


class Logger:
    """Logging utility class

    This class accepts `name` of the logger in its contructor, by default
    `root` will be used if not provided.
    """

    def __init__(self, name="root"):
        self.logger = self.get_logger(name)

    def get_logger(self, name):
        """Returns logger object using configurations and logger name"""
        logging.config.fileConfig('/home/kernelci/config/logger.conf')
        self.logger = logging.getLogger(name)
        return self.logger

    def log_message(self, log_level, msg):
        """Creates a log record with provided log level and message"""
        self.logger.log(log_level, msg)
