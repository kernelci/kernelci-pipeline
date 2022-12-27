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
    def __init__(self, db_handler, db_config_yaml, lab_config, output):
        self._db = db_handler
        self._db_config_yaml = db_config_yaml
        self._runtime = kernelci.lab.get_api(lab_config)
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
            return self._db.submit({'node': node})[0], \
                "Node created successfully"
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            return None, err_msg

    def _generate_job(self, node, plan_config, device_config, tmp):
        """Method to generate jobs"""
        revision = node['revision']
        params = {
            'db_config_yaml': self._db_config_yaml,
            'name': plan_config.name,
            'node_id': node['_id'],
            'revision': revision,
            'runtime': self._runtime.config.lab_type,
            'runtime_image': plan_config.image,
            'tarball_url': node['artifacts']['tarball'],
            'workspace': tmp,
        }
        params.update(plan_config.params)
        params.update(device_config.params)
        templates = ['config/runtime', '/etc/kernelci/runtime']
        job = self._runtime.generate(
            params, device_config, plan_config, templates_paths=templates
        )
        output_file = self._runtime.save_file(job, tmp, params)
        return output_file

    def schedule_job(self, node, plan, device):
        """Generate and schedule jobs"""
        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = self._generate_job(node, plan, device, tmp.name)
        job = self._runtime.submit(output_file)
        return job, tmp
