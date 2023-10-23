#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import logging
import os

import kernelci
from kernelci.api.helper import APIHelper

from logger import Logger


class Service:
    """Common class for pipeline services"""

    def __init__(self, configs, args, name):
        self._name = name
        self._logger = Logger("config/logger.conf", name)
        self._api_config = configs['api'][args.api_config]
        api_token = os.getenv('KCI_API_TOKEN')
        self._api = kernelci.api.get_api(self._api_config, api_token)
        self._api_helper = APIHelper(self._api)

    @property
    def log(self):
        return self._logger

    def _setup(self, args):
        """Set up the service

        Set up the service and return an arbitrary context object.  This will
        then be passed again in `_run()` and `_stop()`.
        """
        return None

    def _stop(self, context):
        """Stop the service

        This is where any resource allocated during _setup() should be
        released.  Please note that if _setup() failed, _stop() is still called
        so checks should be made to determine whether resources were actually
        allocated.
        """
        pass

    def _run(self, context):
        """Implementation method to run the service"""
        raise NotImplementedError("_run() needs to be implemented")

    def run(self, args=None):
        """Interface method to run the service

        Run the service by first calling _setup() to set things up so that
        _stop() can then be called upon termination to release any resources.
        The _run() method is called in between and exceptions are handled
        accordingly.  Any exception except KeyboardInterrupt will result in a
        returned status of False.  The `args` optional arguments are for
        providing extra options, typically parsed from the command line.
        """
        status = True
        context = None

        try:
            context = self._setup(args)
            status = self._run(context)
        except KeyboardInterrupt:
            self.log.info("Stopping.")
        except Exception:
            self.log.traceback()
            status = False
        finally:
            self._stop(context)

        return status
