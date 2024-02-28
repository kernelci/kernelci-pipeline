# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import os
import tempfile

import requests
from flask import Flask, request
import toml
import threading

import kernelci.api.helper
import kernelci.config
import kernelci.runtime.lava
import kernelci.storage


SETTINGS = toml.load(os.getenv('KCI_SETTINGS', 'config/kernelci.toml'))
CONFIGS = kernelci.config.load(
    SETTINGS.get('DEFAULT', {}).get('yaml_config', 'config/pipeline.yaml')
)
SETTINGS_PREFIX = 'runtime'

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
        log_txt.flush()
        # Check size of log file before uploading,
        # if it's empty, don't upload
        if os.path.getsize(log_txt.name) == 0:
            return None
        return storage.upload_single((log_txt.name, 'log.txt'), log_dir)


@app.errorhandler(requests.exceptions.HTTPError)
def handle_http_error(ex):
    detail = ex.response.json().get('detail') or str(ex)
    return detail, ex.response.status_code


@app.route('/')
def hello():
    return "KernelCI API & Pipeline LAVA callback handler"


def async_job_submit(api_helper, node_id, job_callback):
    '''
    Heavy lifting is done in a separate thread to avoid blocking the callback
    handler. This is not ideal as we don't have a way to report errors back to
    the caller, but it's OK as LAVA don't care about the response.
    '''
    print("DEBUG: node_id:", node_id)
    print("DEBUG: job_callback.get_job_status:", job_callback.get_job_status())
    results = job_callback.get_results()
    print("DEBUG: job_callback.get_results:", results)
    job_node = api_helper.api.node.get(node_id)
    # TODO: Verify lab_name matches job node lab name
    # Also extract job_id and compare with node job_id (future)
    # Or at least first record job_id in node metadata

    log_parser = job_callback.get_log_parser()
    job_result = job_callback.get_job_status()
    storage_config_name = job_callback.get_meta('storage_config_name')
    storage = _get_storage(storage_config_name)
    log_txt_url = _upload_log(log_parser, job_node, storage)
    if log_txt_url:
        job_node['artifacts']['lava_log'] = log_txt_url
        print(f"Log uploaded to {log_txt_url}")
    # failed LAVA job should have result set to 'incomplete'
    job_node['result'] = job_result
    job_node['state'] = 'done'
    hierarchy = job_callback.get_hierarchy(results, job_node)
    print("DEBUG: submitting job node", job_node)
    print("DEBUG: submitting hierarchy:", hierarchy)
    api_helper.submit_results(hierarchy, job_node)


@app.post('/node/<node_id>')
def callback(node_id):
    tokens = SETTINGS.get(SETTINGS_PREFIX)
    if not tokens:
        return 'Unauthorized', 401
    lab_token = request.headers.get('Authorization')
    # return 401 if no token
    if not lab_token:
        return 'Unauthorized', 401

    # iterate over tokens and check if value of one matches
    # we might have runtime_token and callback_token
    lab_name = None
    for lab, tokens in tokens.items():
        if tokens.get('runtime_token') == lab_token:
            lab_name = lab
            break
        if tokens.get('callback_token') == lab_token:
            lab_name = lab
            break
    if not lab_name:
        return 'Unauthorized', 401

    data = request.get_json()
    job_callback = kernelci.runtime.lava.Callback(data)
    api_config_name = job_callback.get_meta('api_config_name')
    api_token = os.getenv('KCI_API_TOKEN')
    api_helper = _get_api_helper(api_config_name, api_token)

    # Spawn a thread to do the job submission without blocking
    # the callback
    thread = threading.Thread(
        target=async_job_submit,
        args=(api_helper, node_id, job_callback)
    )
    thread.setDaemon(True)
    thread.start()

    return 'OK', 202


# Default built-in development server, not suitable for production
if __name__ == '__main__':
    tokens = SETTINGS.get(SETTINGS_PREFIX)
    if not tokens:
        print('No tokens configured in toml file')
    app.run(host='0.0.0.0', port=8000)
