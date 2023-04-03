#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

"""KernelCI Job module"""

import os
import tempfile
import json
import requests

import kernelci


class Job():
    """Implements methods for creating and scheduling jobs"""
    def __init__(self, api_helper, api_config_yaml, runtime_config,
                 storage, output):
        self._helper = api_helper
        self._api_config_yaml = api_config_yaml
        self._runtime = kernelci.runtime.get_runtime(runtime_config)
        self._storage = storage
        self._output = output
        self._create_output_dir()

    @property
    def runtime_name(self):
        return self._runtime.config.name

    def get_id(self, job_obj):
        return self._runtime.get_job_id(job_obj)

    def _create_output_dir(self):
        """Create output directory"""
        if not os.path.exists(self._output):
            os.makedirs(self._output)

    def get_device_type(self):
        """Method to get device type"""
        return self._runtime.config.lab_type

    def _generate_job(self, node, job_config, platform_config, tmp):
        """Method to generate jobs"""
        params = self._runtime.get_params(
            node, job_config, platform_config, self._helper.api.config,
            self._storage.config
        )
        job_data = self._runtime.generate(params, job_config)
        output_file = self._runtime.save_file(job_data, tmp, params)
        return output_file

    def schedule_job(self, node, job_config, device):
        """Generate and schedule jobs"""
        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = self._generate_job(node, job_config, device, tmp.name)
        job_obj = self._runtime.submit(output_file)
        return job_obj, tmp
