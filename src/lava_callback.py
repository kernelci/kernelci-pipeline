# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2023-2025 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Denys Fedoryshchenko <denys.fedoryshchenko@collabora.com>

import os
import sys
import tempfile

import gzip
import json
import toml
import yaml
import threading
import uvicorn
import jwt
import logging
import hashlib
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Request, Header, Response
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import kernelci.api.helper
import kernelci.config
import kernelci.runtime.lava
import kernelci.storage
import kernelci.config
from concurrent.futures import ThreadPoolExecutor


from base import validate_url
from telemetry import TelemetryEmitter


SETTINGS = toml.load(os.getenv('KCI_SETTINGS', 'config/kernelci.toml'))
CONFIGS = kernelci.config.load(
    SETTINGS.get('DEFAULT', {}).get('yaml_config', 'config')
)
SETTINGS_PREFIX = 'runtime'
YAMLCFG = kernelci.config.load_yaml('config')

app = FastAPI()
executor = ThreadPoolExecutor(max_workers=16)

# Configure logging to output to stdout
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True  # Force reconfiguration even if logging was already configured
)
logger = logging.getLogger('lava_callback')
logger.setLevel(logging.DEBUG)


class ManualCheckout(BaseModel):
    commit: str
    nodeid: Optional[str] = None
    url: Optional[str] = None
    branch: Optional[str] = None
    jobfilter: Optional[list] = None
    platformfilter: Optional[list] = None


class PatchSet(BaseModel):
    nodeid: str
    patchurl: Optional[list] = None
    patch: Optional[list] = None
    jobfilter: Optional[list] = None
    platformfilter: Optional[list] = None


class JobRetry(BaseModel):
    nodeid: str
    jobfilter: Optional[list] = None


class Metrics():
    def __init__(self, kind):
        self.kind = kind
        self.metrics = {}
        self.metrics['http_requests_total'] = 0
        self.metrics['lava_callback_requests_total'] = 0
        self.metrics['lava_callback_requests_authfail_total'] = 0
        self.metrics['lava_callback_late_fail_total'] = 0
        self.metrics['pipeline_api_auth_fail_total'] = 0
        self.metrics['pipeline_api_requests_total'] = 0
        self.lock = threading.Lock()

    # Various internal metrics
    def update(self):
        with self.lock:
            # This might not work as we have ASGI/WSGI server which have their own threads
            self.metrics['executor_threads_active'] = executor._work_queue.qsize()
            self.metrics['executor_threads_all'] = executor._max_workers

    def add(self, key, value):
        with self.lock:
            if key not in self.metrics:
                self.metrics[key] = 0
            self.metrics[key] += value

    def get(self, key):
        self.update()
        with self.lock:
            return self.metrics.get(key, 0)

    def all(self):
        self.update()
        with self.lock:
            return self.metrics

    def export(self):
        counters = ['_total', '_counter', '_created']
        self.update()
        with self.lock:
            promstr = ''
            for key, value in self.metrics.items():
                promstr += f'# HELP {key} {key}\n'
                # if key doesn't end in _total, _counter - it is a gauge, otherwise counter
                if not any([key.endswith(c) for c in counters]):
                    promstr += f'# TYPE {key} gauge\n'
                else:
                    promstr += f'# TYPE {key} counter\n'
                promstr += f'{key}{{kind="{self.kind}"}} {value}\n'
            return promstr


class LogSanitizer:
    """Efficient log sanitization utility"""

    # Pre-compute translation table
    _TRANSLATION_TABLE = str.maketrans({
        chr(i): '?'
        for i in range(256)
        if not (chr(i).isprintable() or chr(i) == '\n')
    })

    @classmethod
    def sanitize(cls, data: str, max_size: int = None) -> str:
        """
        Sanitize log data by replacing non-printable characters.

        Args:
            data: Input string to sanitize
            max_size: Optional maximum size limit

        Returns:
            Sanitized string
        """
        if not data:
            return ''

        # Truncate if needed
        if max_size and len(data) > max_size:
            data = data[:max_size]

        # Remove null characters and apply translation
        return data.replace('\x00', '').translate(cls._TRANSLATION_TABLE)


metrics = Metrics('pipeline_callback')

# Initialize telemetry emitter with the default API config
_telemetry_emitter = None


def _get_telemetry_emitter():
    """Lazy-init telemetry emitter using default API config."""
    global _telemetry_emitter
    if _telemetry_emitter is None:
        api_config_name = SETTINGS.get('DEFAULT', {}).get('api_config')
        if api_config_name:
            api_token = os.getenv('KCI_API_TOKEN')
            api_config = CONFIGS['api'][api_config_name]
            api = kernelci.api.get_api(api_config, api_token)
            _telemetry_emitter = TelemetryEmitter(
                api, 'lava-callback'
            )
    return _telemetry_emitter


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

            # Sanitize log data to remove non-printable characters
            sanitized_data = LogSanitizer.sanitize(data)

            f.write(sanitized_data)
        src = os.path.join(tmp_dir, 'lava_log.txt.gz')
        return _upload_file(storage, job_node, src, 'log.txt.gz')


def _upload_lava_yaml(log_parser, job_node, storage):
    """Upload structured LAVA log as YAML."""
    id = job_node['id']
    with tempfile.TemporaryDirectory(suffix=id) as tmp_dir:
        yaml_path = os.path.join(tmp_dir, 'lava-logs.yaml')
        try:
            log_data = log_parser.get_data()
            if not log_data:
                return None

            with open(yaml_path, 'w') as f:
                yaml.dump(log_data, f, default_flow_style=False)

            return _upload_file(storage, job_node, yaml_path, 'lava-logs.yaml')
        except Exception as e:
            logger.warning(f"Failed to upload lava-logs.yaml: {e}")
            return None


@app.get('/', response_class=HTMLResponse)
async def read_root():
    page = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>KernelCI Pipeline endpoint</title>
        <style>
            body { font-family: Arial, sans-serif; background: #f8f9fa; color: #222; margin: 0; padding: 0; }
            .container { max-width: 600px; margin: 60px auto; background: #fff; border-radius: 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.07); padding: 32px; }
            h1 { color: #0057b7; }
            a { color: #0057b7; text-decoration: none; }
            a:hover { text-decoration: underline; }
            .logo { width: 120px; margin-bottom: 24px; }
        </style>
    </head>
    <body>
        <div class="container">
            <img src="https://kernelci.org/wp-content/uploads/sites/92/2019/10/kernelci-horizontal-color.svg" alt="KernelCI Logo" class="logo"/>
            <h1>KernelCI Pipeline endpoint</h1>
            <p>Welcome! This is the <b>KernelCI Pipeline endpoint</b> REST API endpoint.</p>
            <p>This service receives and processes callback data from LAVA and other test systems as part of the KernelCI continuous integration pipeline.</p>
            <p>Also, it is used by kci-dev to interact with the pipeline.</p>
            <p>For more information about KernelCI, please visit <a href="https://kernelci.org/" target="_blank">kernelci.org</a>.</p>
            <p>API usage and integration details can be found in the <a href="https://docs.kernelci.org/" target="_blank">KernelCI documentation</a>.</p>
            <hr/>
            <p style="font-size: 0.95em; color: #888;">&copy; 2025 KernelCI Project</p>
        </div>
    </body>
    </html>
    '''
    return HTMLResponse(content=page)


def async_job_submit(api_helper, node_id, job_callback):
    '''
    Heavy lifting is done in a separate thread to avoid blocking the callback
    handler. This is not ideal as we don't have a way to report errors back to
    the caller, but it's OK as LAVA don't care about the response.
    '''
    try:
        logger.info(f'Starting async job submit for node {node_id}')
        results = job_callback.get_results()
        job_node = api_helper.api.node.get(node_id)
        if not job_node:
            metrics.add('lava_callback_late_fail_total', 1)
            logger.error(f'Node {node_id} not found')
            return
        logger.debug(f'Processing node {node_id}: {job_node.get("name", "unknown")}')

        # TODO: Verify lab_name matches job node lab name
        # Also extract job_id and compare with node job_id (future)
        # Or at least first record job_id in node metadata

        callback_data = job_callback.get_data()
        log_parser = job_callback.get_log_parser()
        job_result = job_callback.get_job_status()
        device_id = job_callback.get_device_id()
        storage_config_name = job_callback.get_meta('storage_config_name')
        storage = _get_storage(storage_config_name)

        if log_parser:
            log_txt_url = _upload_log(log_parser, job_node, storage)
            if log_txt_url:
                job_node['artifacts']['lava_log'] = log_txt_url
                logger.info(f"Log uploaded to {log_txt_url}")
            else:
                logger.warning("Failed to upload log")
                metrics.add('lava_callback_late_fail_total', 1)
            lava_yaml_url = _upload_lava_yaml(log_parser, job_node, storage)
            if lava_yaml_url:
                job_node['artifacts']['lava_logs'] = lava_yaml_url
                logger.info(f"LAVA YAML uploaded to {lava_yaml_url}")
            callback_json_url = _upload_callback_data(callback_data, job_node, storage)
            if callback_json_url:
                job_node['artifacts']['callback_data'] = callback_json_url
                logger.info(f"Callback data uploaded to {callback_json_url}")
            else:
                metrics.add('lava_callback_late_fail_total', 1)
        else:
            logger.warning("No log data found in callback")
            job_result = 'incomplete'
        # failed LAVA job should have result set to 'incomplete'
        job_node['result'] = job_result
        job_node['state'] = 'done'
        if job_node.get('error_code') == 'node_timeout':
            job_node['error_code'] = None
            job_node['error_msg'] = None
        if device_id:
            job_node['data']['device'] = device_id
        # add artifacts uploaded from the running LAVA job
        upload_result = results.pop('upload', {})
        for (name, state) in upload_result.items():
            if name.startswith("artifact-upload:") and state == 'pass':
                artifact = name.split(':', 2)
                if len(artifact) != 3:
                    logger.warn(f"Failed to extract artifact name and URL from {name}")
                    continue
                job_node['artifacts'][artifact[1]] = artifact[2]
                logger.info(f"Artifact {artifact[1]} added with URL {artifact[2]}")
        hierarchy = job_callback.get_hierarchy(results, job_node)
        api_helper.submit_results(hierarchy, job_node)

        # Emit telemetry events
        _emit_callback_telemetry(
            job_node, job_callback, hierarchy
        )

        logger.info(f"Completed processing callback for node {node_id}")
    except Exception as e:
        logger.exception(f"Error processing callback for node {node_id}: {e}")
        metrics.add('lava_callback_late_fail_total', 1)


def _emit_callback_telemetry(job_node, job_callback, hierarchy):
    """Emit telemetry for LAVA callback results."""
    emitter = _get_telemetry_emitter()
    if not emitter:
        return

    kernel_rev = job_node.get('data', {}).get('kernel_revision', {})
    runtime = job_node.get('data', {}).get('runtime', '')
    device_type = job_node.get('data', {}).get('platform', '')
    device_id = job_node.get('data', {}).get('device')
    job_id = job_node.get('data', {}).get('job_id')
    is_infra = job_callback.is_infra_error() if job_node.get(
        'result') == 'incomplete' else False

    common = {
        'runtime': runtime,
        'device_type': device_type,
        'device_id': device_id,
        'job_name': job_node.get('name', ''),
        'job_id': str(job_id) if job_id else None,
        'node_id': job_node.get('id'),
        'tree': kernel_rev.get('tree'),
        'branch': kernel_rev.get('branch'),
        'arch': job_node.get('data', {}).get('arch'),
    }

    # Job-level result
    emitter.emit(
        'job_result',
        result=job_node.get('result'),
        is_infra_error=is_infra,
        error_type=job_node.get('data', {}).get('error_code'),
        error_msg=job_node.get('data', {}).get('error_msg'),
        **common,
    )

    # Per-test results from hierarchy
    child_nodes = hierarchy.get('child_nodes', [])
    _emit_test_results(emitter, child_nodes, common)


def _emit_test_results(emitter, child_nodes, common, suite=None):
    """Recursively emit test_result events from the hierarchy."""
    for child in child_nodes:
        node = child.get('node', {})
        name = node.get('name', '')
        kind = node.get('kind', 'test')
        result = node.get('result')
        sub_children = child.get('child_nodes', [])

        if kind == 'test':
            emitter.emit(
                'test_result',
                test_name=name,
                result=result,
                extra={'suite': suite} if suite else {},
                **common,
            )
        elif sub_children:
            _emit_test_results(
                emitter, sub_children, common, suite=name
            )


def submit_job(api_helper, node_id, job_callback):
    '''
    Spawn a thread to do the job submission without blocking
    the callback
    '''
    executor.submit(async_job_submit, api_helper, node_id, job_callback)


# POST /node/<node_id>
@app.post('/node/{node_id}')
async def callback(node_id: str, request: Request):
    logger.info(f"Received callback for node {node_id}")
    metrics.add('http_requests_total', 1)
    metrics.add('lava_callback_requests_total', 1)
    tokens = SETTINGS.get(SETTINGS_PREFIX)
    if not tokens:
        item = {}
        item['message'] = 'No tokens configured'
        return JSONResponse(content=item, status_code=500)
    lab_token = request.headers.get('Authorization')
    # return 401 if no token
    if not lab_token:
        metrics.add('lava_callback_requests_authfail_total', 1)
        item = {}
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

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
        metrics.add('lava_callback_requests_authfail_total', 1)
        item = {}
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

    try:
        data = await request.json()
    except Exception as e:
        logger.error(f'Error decoding JSON: {e}')
        item = {}
        item['message'] = 'Error decoding JSON'
        return JSONResponse(content=item, status_code=400)
    job_callback = kernelci.runtime.lava.Callback(data)
    api_config_name = job_callback.get_meta('api_config_name')
    api_token = os.getenv('KCI_API_TOKEN')
    api_helper = _get_api_helper(api_config_name, api_token)

    submit_job(api_helper, node_id, job_callback)

    item = {}
    item['message'] = 'OK'
    return JSONResponse(content=item, status_code=202)


def decode_jwt(jwtstr):
    '''
    JWT secret stored at SETTINGS['jwt']['secret']
    which means secret.toml file should have jwt section
    with parameter secret= "<secret>"
    '''
    secret = SETTINGS.get('jwt', {}).get('secret')
    if not secret:
        logger.error('No JWT secret configured')
        return None
    return jwt.decode(jwtstr, secret, algorithms=['HS256'])


def validate_permissions(jwtoken, permission):
    if not jwtoken:
        return False
    try:
        decoded = decode_jwt(jwtoken)
    except Exception as e:
        logger.error(f'Error decoding JWT: {e}')
        return False
    if not decoded:
        logger.error('Invalid JWT')
        return False
    permissions = decoded.get('permissions')
    if not permissions:
        logger.error('No permissions in JWT')
        return False
    if permission not in permissions:
        logger.error(f'Permission {permission} not in JWT')
        return False
    return decoded


def find_parent_kind(node, api_helper, kind):
    '''
    Find parent node of a specific "kind" value
    '''
    parent_id = node.get('parent')
    if not parent_id:
        return None
    parent_node = api_helper.api.node.get(parent_id)
    if not parent_node:
        return None
    if parent_node.get('kind') == kind:
        return parent_node
    return find_parent_kind(parent_node, api_helper, kind)


def find_tree(url, branch):
    '''
    Find tree name from the URL and branch
    '''
    treename = None
    for tree in YAMLCFG['trees']:
        data = YAMLCFG['trees'].get(tree)
        if data.get('url') == url:
            treename = tree

    if not treename:
        return None

    for bconfig in YAMLCFG['build_configs']:
        data = YAMLCFG['build_configs'].get(bconfig)
        if data.get('tree') == treename:
            return treename

    return None


@app.post('/api/jobretry')
async def jobretry(data: JobRetry, request: Request,
                   Authorization: str = Header(None)):
    '''
    API call to assist in regression bisecting by retrying a specific job
    retrieved from test results.
    '''
    metrics.add('http_requests_total', 1)
    metrics.add('pipeline_api_requests_total', 1)
    # return item
    item = {}
    # Validate JWT token from Authorization header
    jwtoken = Authorization
    decoded = validate_permissions(jwtoken, 'testretry')
    if not decoded:
        metrics.add('pipeline_api_auth_fail_total', 1)
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

    email = decoded.get('email')
    logger.info(f"User {email} is retrying job {data.nodeid}")
    api_config_name = SETTINGS.get('DEFAULT', {}).get('api_config')
    if not api_config_name:
        item['message'] = 'No default API name set'
        return JSONResponse(content=item, status_code=500)
    api_token = os.getenv('KCI_API_TOKEN')
    api_helper = _get_api_helper(api_config_name, api_token)
    try:
        node = api_helper.api.node.get(data.nodeid)
    except Exception as e:
        logger.error(f'Error getting node {data.nodeid}: {e}')
        item['message'] = 'Error getting node'
        return JSONResponse(content=item, status_code=500)
    if not node:
        item['message'] = 'Node not found'
        return JSONResponse(content=item, status_code=404)
    if node['kind'] != 'job':
        item['message'] = 'Node is not a job'
        return JSONResponse(content=item, status_code=400)

    knode = find_parent_kind(node, api_helper, 'kbuild')
    if not knode:
        item['message'] = 'Kernel build not found'
        return JSONResponse(content=item, status_code=404)

    jobfilter = [knode['name'], node['name']]
    knode['jobfilter'] = jobfilter
    if data.jobfilter:
        knode['jobfilter'].extend(data.jobfilter)
    knode['op'] = 'updated'
    knode['data'].pop('artifacts', None)
    # state - done, result - pass
    if knode.get('state') != 'done':
        item['message'] = 'Kernel build is not done'
        return JSONResponse(content=item, status_code=400)
    if knode.get('result') != 'pass':
        item['message'] = 'Kernel build result is not pass'
        return JSONResponse(content=item, status_code=400)
    # remove created, updated, timeout, owner, submitter, usergroups
    knode.pop('created', None)
    knode.pop('updated', None)
    knode.pop('timeout', None)
    knode.pop('owner', None)
    knode.pop('submitter', None)
    knode.pop('usergroups', None)

    evnode = {'data': knode}
    # Now we can submit custom kbuild node to the API(pub/sub)
    api_helper.api.send_event('node', evnode)
    logger.info(f"Job retry for node {data.nodeid} submitted")
    item['message'] = 'OK'
    return JSONResponse(content=item, status_code=200)


def get_jobfilter(node, api_helper):
    jobfilter = []
    if node['kind'] != 'job':
        jobnode = find_parent_kind(node, api_helper, 'job')
        if not jobnode:
            return None
    else:
        jobnode = node

    kbuildnode = find_parent_kind(node, api_helper, 'kbuild')
    if not kbuildnode:
        return None

    kbuildname = kbuildnode['name']
    testname = jobnode['name']
    jobfilter = [kbuildname, testname]
    return jobfilter


def is_valid_commit_string(commit):
    '''
    Validate commit string format
    '''
    if not commit:
        return False
    if len(commit) < 7:
        return False
    if len(commit) > 40:
        return False
    if not all(c in '0123456789abcdef' for c in commit):
        return False
    return True


def is_job_exist(jobname):
    '''
    Check if job exists in the config
    '''
    for job in YAMLCFG['jobs']:
        if job == jobname:
            return True
    return False


def is_platform_exist(platform):
    '''
    Check if platform exists in the config
    '''
    for p in YAMLCFG['platforms']:
        if p == platform:
            return True
    return False


@app.post('/api/checkout')
async def checkout(data: ManualCheckout, request: Request,
                   Authorization: str = Header(None)):
    '''
    API call to assist in regression bisecting by manually checking out
    a specific commit on a specific branch of a specific tree, retrieved
    from test results.

    User either supplies a node ID to checkout, or a tree URL, branch and
    commit hash. In the latter case, the tree name is looked up in the
    configuration file.
    '''
    metrics.add('http_requests_total', 1)
    metrics.add('pipeline_api_requests_total', 1)
    item = {}
    # Validate JWT token from Authorization header
    jwtoken = Authorization
    decoded = validate_permissions(jwtoken, 'checkout')
    if not decoded:
        metrics.add('pipeline_api_auth_fail_total', 1)
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

    email = decoded.get('email')
    if not email:
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

    logger.info(f"User {email} is checking out {data.nodeid} at custom commit {data.commit}")
    api_config_name = SETTINGS.get('DEFAULT', {}).get('api_config')
    if not api_config_name:
        item['message'] = 'No default API name set'
        return JSONResponse(content=item, status_code=500)
    api_token = os.getenv('KCI_API_TOKEN')
    api_helper = _get_api_helper(api_config_name, api_token)

    # if user set node - we retrieve all the tree data from it
    if data.nodeid:
        node = api_helper.api.node.get(data.nodeid)
        # validate commit string
        if not is_valid_commit_string(data.commit):
            item['message'] = 'Invalid commit format'
            return JSONResponse(content=item, status_code=400)
        if not node:
            item['message'] = 'Node not found'
            return JSONResponse(content=item, status_code=404)
        try:
            treename = node['data']['kernel_revision']['tree']
            treeurl = node['data']['kernel_revision']['url']
            branch = node['data']['kernel_revision']['branch']
            commit = data.commit
        except KeyError:
            item['message'] = 'Node does not have kernel revision data'
            return JSONResponse(content=item, status_code=400)

        jobfilter = get_jobfilter(node, api_helper)
        # TBD: platformfilter
    else:
        if not data.url or not data.branch or not data.commit:
            item['message'] = 'Missing tree URL, branch or commit'
            return JSONResponse(content=item, status_code=400)
        if not is_valid_commit_string(data.commit):
            item['message'] = 'Invalid commit format'
            return JSONResponse(content=item, status_code=400)
        treename = find_tree(data.url, data.branch)
        if not treename:
            item['message'] = 'Tree not found'
            return JSONResponse(content=item, status_code=404)
        treeurl = data.url
        branch = data.branch
        commit = data.commit

        # validate jobfilter list
        if data.jobfilter:
            # to be on safe side restrict length of jobfilter to 8
            if len(data.jobfilter) > 8:
                item['message'] = 'Too many jobs in jobfilter'
                return JSONResponse(content=item, status_code=400)
            # jobfilter entries can be suffixed with '+', meaning this
            # job and all of its child jobs are allowed; we should
            # therefore drop the suffix when checking if job exists
            for jobname in (f.rstrip('+') for f in data.jobfilter):
                if not is_job_exist(jobname):
                    item['message'] = f'Job {jobname} not found'
                    return JSONResponse(content=item, status_code=404)
            jobfilter = data.jobfilter
        else:
            jobfilter = None

        if data.platformfilter:
            # to be on safe side restrict length of platformfilter to 8
            if len(data.platformfilter) > 8:
                item['message'] = 'Too many platforms in platformfilter'
                return JSONResponse(content=item, status_code=400)
            for platform in data.platformfilter:
                if not is_platform_exist(platform):
                    item['message'] = f'Platform {platform} not found'
                    return JSONResponse(content=item, status_code=404)
            platform_filter = data.platformfilter
        else:
            platform_filter = None

    # Now we can submit custom checkout node to the API
    # Maybe add field who requested the checkout?
    timeout = 300
    checkout_timeout = datetime.utcnow() + timedelta(minutes=timeout)
    treeidsrc = treeurl + branch + str(datetime.now())
    treeid = hashlib.sha256(treeidsrc.encode()).hexdigest()
    node = {
        "kind": "checkout",
        "name": "checkout",
        "path": ["checkout"],
        "data": {
            "kernel_revision": {
                "tree": treename,
                "branch": branch,
                "commit": commit,
                "url": treeurl,
                "tip_of_branch": False
            }
        },
        "timeout": checkout_timeout.isoformat(),
        "submitter": f'user:{email}',
        "treeid": treeid,
    }

    if jobfilter:
        node['jobfilter'] = jobfilter

    if platform_filter:
        node['platform_filter'] = platform_filter

    r = api_helper.api.node.add(node)
    if not r:
        item['message'] = 'Failed to submit checkout node'
        return JSONResponse(content=item, status_code=500)
    else:
        logger.info(f"Checkout node {r['id']} submitted")
        item['message'] = 'OK'
        item['node'] = r
        return JSONResponse(content=item, status_code=200)


@app.post('/api/patchset')
async def patchset(data: PatchSet, request: Request,
                   Authorization: str = Header(None)):
    '''
    API call to test existing checkout with a patch(set)
    Patch can be supplied as a URL or within the request body
    '''
    metrics.add('http_requests_total', 1)
    metrics.add('pipeline_api_requests_total', 1)
    item = {}
    # Validate JWT token from Authorization header
    jwtoken = Authorization
    decoded = validate_permissions(jwtoken, 'patchset')
    if not decoded:
        metrics.add('pipeline_api_auth_fail_total', 1)
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

    email = decoded.get('email')
    if not email:
        item['message'] = 'Unauthorized'
        return JSONResponse(content=item, status_code=401)

    logger.info(f"User {email} is testing patchset on {data.nodeid}")
    api_config_name = SETTINGS.get('DEFAULT', {}).get('api_config')
    if not api_config_name:
        item['message'] = 'No default API name set'
        return JSONResponse(content=item, status_code=500)
    api_token = os.getenv('KCI_API_TOKEN')
    api_helper = _get_api_helper(api_config_name, api_token)

    node = api_helper.api.node.get(data.nodeid)
    if not node:
        item['message'] = 'Node not found'
        return JSONResponse(content=item, status_code=404)
    if node['kind'] != 'checkout':
        item['message'] = 'Node is not a checkout'
        return JSONResponse(content=item, status_code=400)

    # validate patch URL
    if data.patchurl:
        if isinstance(data.patchurl, list):
            for patchurl in data.patchurl:
                if not isinstance(patchurl, str):
                    item['message'] = 'Invalid patch URL element type'
                    return JSONResponse(content=item, status_code=400)
                if not validate_url(patchurl):
                    item['message'] = 'Invalid patch URL'
                    return JSONResponse(content=item, status_code=400)
        else:
            return 'Invalid patch URL type', 400
    elif data.patch:
        # We need to implement upload to storage and return URL
        item['message'] = 'Not implemented yet'
        return JSONResponse(content=item, status_code=501)
    else:
        item['message'] = 'Missing patch URL or patch'
        return JSONResponse(content=item, status_code=400)

    # Now we can submit custom patchset node to the API
    # Maybe add field who requested the patchset?
    timeout = 300
    patchset_timeout = datetime.utcnow() + timedelta(minutes=timeout)
    treeidsrc = node['data']['kernel_revision']['url'] + \
        node['data']['kernel_revision']['branch'] + str(datetime.now())
    treeid = hashlib.sha256(treeidsrc.encode()).hexdigest()
    # copy node to newnode
    newnode = node.copy()
    # delete some fields, like id, created, updated, timeout
    newnode.pop('id', None)
    newnode.pop('created', None)
    newnode.pop('updated', None)
    newnode.pop('timeout', None)
    newnode.pop('result', None)
    newnode.pop('owner', None)
    newnode['name'] = 'patchset'
    newnode['path'] = ['checkout', 'patchset']
    newnode['group'] = 'patchset'
    newnode['state'] = 'running'
    newnode['parent'] = node['id']
    newnode['artifacts'] = {}
    newnode['timeout'] = patchset_timeout.isoformat()
    newnode['submitter'] = f'user:{email}'
    newnode['treeid'] = treeid
    if data.patchurl:
        for i, patchurl in enumerate(data.patchurl):
            newnode['artifacts'][f'patch{i}'] = patchurl
    if data.jobfilter:
        newnode['jobfilter'] = data.jobfilter
    if data.platformfilter:
        newnode['platform_filter'] = data.platformfilter

    r = api_helper.api.node.add(newnode)
    if not r:
        item['message'] = 'Failed to submit patchset node'
        return JSONResponse(content=item, status_code=500)
    else:
        logger.info(f"Patchset node {r['id']} submitted")
        item['message'] = 'OK'
        item['node'] = r
        return JSONResponse(content=item, status_code=200)


@app.get('/api/metrics')
async def apimetrics():
    '''
    Prometheus compatible metrics export
    /api/metrics
    http_requests_total{kind="pipeline_callback"} 4633433
    lava_callback_requests_total{kind="pipeline_callback"} 4633433
    lava_callback_requests_authfail{kind="pipeline_callback"} 0
    lava_callback_late_fail{kind="pipeline_callback"} 0
    '''
    metrics.add('http_requests_total', 1)
    export_str = metrics.export()

    return Response(content=export_str, media_type='text/plain')


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logging.error(f"Unhandled exception: {exc}")
    return JSONResponse(
        status_code=500,
        content={"message": "Internal server error"}
    )


# Default built-in development server, not suitable for production
if __name__ == '__main__':
    tokens = SETTINGS.get(SETTINGS_PREFIX)
    if not tokens:
        print('No tokens configured in toml file')
    jwt_secret = SETTINGS.get('jwt', {}).get('secret')
    if not jwt_secret:
        print('No JWT secret configured')
        sys.exit(1)
    api_token = os.getenv('KCI_API_TOKEN')
    if not api_token:
        print('No API token set')
        sys.exit(1)
    logger.info("Starting LAVA callback server...")
    uvicorn.run(app, host='0.0.0.0', port=8000, log_level="debug")
