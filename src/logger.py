#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

"""Logger module"""

import logging
import logging.config
import traceback
import time


class Logger:
    """Logging utility class

    This class accepts name and path of the configuration file of the logger
    in its contructor, by default `root` will be used as name of the logger
    if not provided.
    """

    def __init__(self, config_path, name='root'):
        """Returns logger object using configurations and logger name"""
        log_file_name = f'{name}_{time.strftime("%Y_%m_%d-%H:%M:%S")}.log'
        logging.config.fileConfig(config_path,
                                  defaults={'log_file': log_file_name})
        self._logger = logging.getLogger(name)

    def log_message(self, log_level, msg):
        """Creates a log record with provided log level and message"""
        self._logger.log(log_level, msg)

    def debug(self, msg):
        self.log_message(logging.DEBUG, msg)

    def info(self, msg):
        self.log_message(logging.INFO, msg)

    def warning(self, msg):
        self.log_message(logging.WARNING, msg)

    def error(self, msg):
        self.log_message(logging.ERROR, msg)

    def critical(self, msg):
        self.log_message(logging.CRITICAL, msg)

    def traceback(self):
        self.error(traceback.format_exc())
