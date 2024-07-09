# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import os
import tempfile

import gzip
import json
import requests
import toml
import threading
import uvicorn
from fastapi import FastAPI, HTTPException, Request

import kernelci.api.helper
import kernelci.config
import kernelci.runtime.lava
import kernelci.storage
from concurrent.futures import ThreadPoolExecutor


SETTINGS = toml.load(os.getenv('KCI_SETTINGS', 'config/kernelci.toml'))
CONFIGS = kernelci.config.load(
    SETTINGS.get('DEFAULT', {}).get('yaml_config', 'config')
)
SETTINGS_PREFIX = 'runtime'

app = FastAPI()
executor = ThreadPoolExecutor(max_workers=16)


def _get_api_helper(api_config_name, api_token):
    api_config = CONFIGS['api'][api_config_name]
    api = kernelci.api.get_api(api_config, api_token)
    return kernelci.api.helper.APIHelper(api)


def _get_storage(storage_config_name):
    storage_config = CONFIGS['storage_configs'][storage_config_name]
    storage_cred = SETTINGS['storage'][storage_config_name]['storage_cred']
    return kernelci.storage.get_storage(storage_config, storage_cred)


def _upload_file(storage, job_node, source_name, destination_name=None):
    if not destination_name:
        destination_name = source_name
    upload_dir = '-'.join((job_node['name'], job_node['id']))
    # remove GET parameters from destination_name
    return storage.upload_single((source_name, destination_name), upload_dir)


def _upload_callback_data(data, job_node, storage):
    filename = 'lava_callback.json.gz'
    # Temporarily we dont remove log field
    # data.pop('log', None)
    # Ensure we don't leak secrets
    data.pop('token', None)
    # Create temporary file to store callback data as gzip'ed JSON
    with tempfile.TemporaryDirectory() as tmp_dir:
        # open gzip in explicit text mode to avoid platform-dependent line endings
        with gzip.open(os.path.join(tmp_dir, filename), 'wt') as f:
            serjson = json.dumps(data, indent=4)
            f.write(serjson)
        src = os.path.join(tmp_dir, filename)
        return _upload_file(storage, job_node, src, filename)


def _upload_log(log_parser, job_node, storage):
    # create temporary file to store log with gzip
    id = job_node['id']
    with tempfile.TemporaryDirectory(suffix=id) as tmp_dir:
        # open gzip in explicit text mode to avoid platform-dependent line endings
        with gzip.open(os.path.join(tmp_dir, 'lava_log.txt.gz'), 'wt') as f:
            data = log_parser.get_text()
            if not data or len(data) == 0:
                return None
            # Delete NULL characters from log data
            data = data.replace('\x00', '')
            # Sanitize log data from non-printable characters (except newline)
            # replace them with '?', original still exists in cb data
            data = ''.join([c if c.isprintable() or c == '\n' else
                            '?' for c in data])
            f.write(data)
        src = os.path.join(tmp_dir, 'lava_log.txt.gz')
        return _upload_file(storage, job_node, src, 'log.txt.gz')


@app.get('/')
async def read_root():
    page = '''
    <html>
    <head>
    <title>KernelCI Pipeline Callback</title>
    </head>
    <body>
    <h1>KernelCI Pipeline Callback</h1>
    <p>This is a callback endpoint for the KernelCI pipeline.</p>
    </body>
    </html>
    '''
    return page


def async_job_submit(api_helper, node_id, job_callback):
    '''
    Heavy lifting is done in a separate thread to avoid blocking the callback
    handler. This is not ideal as we don't have a way to report errors back to
    the caller, but it's OK as LAVA don't care about the response.
    '''
    results = job_callback.get_results()
    job_node = api_helper.api.node.get(node_id)
    if not job_node:
        print(f'Node {node_id} not found')
        return
    # TODO: Verify lab_name matches job node lab name
    # Also extract job_id and compare with node job_id (future)
    # Or at least first record job_id in node metadata

    callback_data = job_callback.get_data()
    log_parser = job_callback.get_log_parser()
    job_result = job_callback.get_job_status()
    device_id = job_callback.get_device_id()
    storage_config_name = job_callback.get_meta('storage_config_name')
    storage = _get_storage(storage_config_name)
    log_txt_url = _upload_log(log_parser, job_node, storage)
    if log_txt_url:
        job_node['artifacts']['lava_log'] = log_txt_url
        print(f"Log uploaded to {log_txt_url}")
    callback_json_url = _upload_callback_data(callback_data, job_node, storage)
    if callback_json_url:
        job_node['artifacts']['callback_data'] = callback_json_url
        print(f"Callback data uploaded to {callback_json_url}")
    # failed LAVA job should have result set to 'incomplete'
    job_node['result'] = job_result
    job_node['state'] = 'done'
    if job_node.get('error_code') == 'node_timeout':
        job_node['error_code'] = None
        job_node['error_msg'] = None
    if device_id:
        job_node['data']['device'] = device_id
    hierarchy = job_callback.get_hierarchy(results, job_node)
    api_helper.submit_results(hierarchy, job_node)


def submit_job(api_helper, node_id, job_callback):
    '''
    Spawn a thread to do the job submission without blocking
    the callback
    '''
    executor.submit(async_job_submit, api_helper, node_id, job_callback)


# POST /node/<node_id>
@app.post('/node/{node_id}')
async def callback(node_id: str, request: Request):
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

    data = await request.json()
    job_callback = kernelci.runtime.lava.Callback(data)
    api_config_name = job_callback.get_meta('api_config_name')
    api_token = os.getenv('KCI_API_TOKEN')
    api_helper = _get_api_helper(api_config_name, api_token)

    submit_job(api_helper, node_id, job_callback)

    return 'OK', 202


# Default built-in development server, not suitable for production
if __name__ == '__main__':
    tokens = SETTINGS.get(SETTINGS_PREFIX)
    if not tokens:
        print('No tokens configured in toml file')
    uvicorn.run(app, host='0.0.0.0', port=8000)
