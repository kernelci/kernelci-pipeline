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
    def __init__(self, api_handler, api_config_yaml, runtime_config, output):
        self._api = api_handler
        self._api_config_yaml = api_config_yaml
        self._runtime = kernelci.runtime.get_runtime(runtime_config)
        self._output = output
        self._create_output_dir()

    def _create_output_dir(self):
        """Create output directory"""
        if not os.path.exists(self._output):
            os.makedirs(self._output)

    def get_device_type(self):
        """Method to get device type"""
        return self._runtime.config.lab_type

    def create_node(self, checkout_node, plan_config):
        """Method to generate node for the job"""
        node = {
            'parent': checkout_node['_id'],
            'name': plan_config.name,
            'path': checkout_node['path'] + [plan_config.name],
            'group': plan_config.name,
            'artifacts': checkout_node['artifacts'],
            'revision': checkout_node['revision'],
        }
        try:
            return self._api.submit({'node': node})[0], \
                "Node created successfully"
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            return None, err_msg

    def _generate_job(self, node, plan_config, platform_config, tmp):
        """Method to generate jobs"""
        params = self._runtime.get_params(
            node, plan_config, platform_config, self._api.config
        )
        job = self._runtime.generate(params, plan_config)
        output_file = self._runtime.save_file(job, tmp, params)
        return output_file

    def schedule_job(self, node, plan, device):
        """Generate and schedule jobs"""
        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = self._generate_job(node, plan, device, tmp.name)
        job = self._runtime.submit(output_file)
        return job, tmp
