# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import os

import kernelci.api.helper
import kernelci.config
from kernelci.runtime.lava import Callback

from flask import Flask, request


def _get_api_helper():
    configs = kernelci.config.load('config/pipeline.yaml')
    api_config = configs['api_configs']['staging.kernelci.org']
    api_token = os.getenv('KCI_API_TOKEN')
    api = kernelci.api.get_api(api_config, api_token)
    return kernelci.api.helper.APIHelper(api)


api_helper = _get_api_helper()
app = Flask(__name__)


@app.route('/')
def hello():
    return "KernelCI API & Pipeline LAVA callback handler"


@app.post('/node/<node_id>')
def callback(node_id):
    data = request.get_json()
    job_callback = Callback(data)
    results = job_callback.get_results()
    job_node = api_helper.api.get_node(node_id)
    hierarchy = job_callback.get_hierarchy(results, job_node)
    return api_helper.submit_results(hierarchy, job_node)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
