# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import os
import tempfile

import requests
from flask import Flask, request
import toml

import kernelci.api.helper
import kernelci.config
import kernelci.runtime.lava
import kernelci.storage

SETTINGS = toml.load(os.getenv('KCI_SETTINGS', 'config/kernelci.toml'))
CONFIGS = kernelci.config.load(
    SETTINGS.get('DEFAULT', {}).get('yaml_config', 'config/pipeline.yaml')
)

app = Flask(__name__)


def _get_api_helper(api_config_name, api_token):
    api_config = CONFIGS['api'][api_config_name]
    api = kernelci.api.get_api(api_config, api_token)
    return kernelci.api.helper.APIHelper(api)


def _get_storage(storage_config_name):
    storage_config = CONFIGS['storage_configs'][storage_config_name]
    storage_cred = SETTINGS['storage'][storage_config_name]['storage_cred']
    return kernelci.storage.get_storage(storage_config, storage_cred)


def _upload_log(log_parser, job_node, storage):
    with tempfile.NamedTemporaryFile(mode='w') as log_txt:
        log_parser.get_text_log(log_txt)
        os.chmod(log_txt.name, 0o644)
        log_dir = '-'.join((job_node['name'], job_node['id']))
        return storage.upload_single((log_txt.name, 'log.txt'), log_dir)


@app.errorhandler(requests.exceptions.HTTPError)
def handle_http_error(ex):
    detail = ex.response.json().get('detail') or str(ex)
    return detail, ex.response.status_code


@app.route('/')
def hello():
    return "KernelCI API & Pipeline LAVA callback handler"


@app.post('/node/<node_id>')
def callback(node_id):
    data = request.get_json()
    job_callback = kernelci.runtime.lava.Callback(data)

    api_config_name = job_callback.get_meta('api_config_name')
    api_token = request.headers.get('Authorization')
    api_helper = _get_api_helper(api_config_name, api_token)
    results = job_callback.get_results()
    job_node = api_helper.api.get_node(node_id)

    log_parser = job_callback.get_log_parser()
    storage_config_name = job_callback.get_meta('storage_config_name')
    storage = _get_storage(storage_config_name)
    log_txt_url = _upload_log(log_parser, job_node, storage)
    job_node['artifacts']['log.txt'] = log_txt_url

    hierarchy = job_callback.get_hierarchy(results, job_node)
    return api_helper.submit_results(hierarchy, job_node)


# Default built-in development server, not suitable for production
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
