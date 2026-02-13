#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022, 2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import os
import sys
import tempfile
import json
import yaml
import requests
import re
import datetime
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import shutil
import traceback
import signal

import kernelci
import kernelci.config
import kernelci.context
import kernelci.runtime
import kernelci.scheduler
import kernelci.storage
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service
from telemetry import TelemetryEmitter

BACKUP_DIR = '/tmp/kci-backup'
WATCHDOG_TIMEOUT = 10 * 60  # 10 minutes in seconds


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/health':
            # Simple OK response - service is running
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK\n")
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found\n")


def run_health_server():
    server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
    server.serve_forever()


def run_watchdog(scheduler_instance, logger):
    """Monitor scheduler threads and crash if any thread is stuck for too long"""
    logger.info(f"Watchdog started with {WATCHDOG_TIMEOUT}s timeout")
    check_interval = 30  # Check every 30 seconds

    while True:
        with scheduler_instance._stop_thread_lock:
            if scheduler_instance._stop_thread:
                logger.info("Watchdog stopping")
                break

        time.sleep(check_interval)
        current_time = time.time()

        with scheduler_instance._watchdog_lock:
            timestamps = scheduler_instance._watchdog_timestamps.copy()

        # Check each thread's timestamp
        for thread_name, last_update in timestamps.items():
            time_since_update = current_time - last_update
            if time_since_update > WATCHDOG_TIMEOUT:
                logger.error(f"WATCHDOG: Thread '{thread_name}' stuck for {time_since_update:.0f}s "
                             f"(timeout: {WATCHDOG_TIMEOUT}s). Forcing immediate exit!")

                # Try to print minimal info - avoid complex operations that might also hang
                try:
                    logger.error("=" * 80)
                    logger.error("STUCK THREAD DETECTED - FORCING EXIT")
                    logger.error(f"Thread: {thread_name}, stuck for {time_since_update:.0f}s")
                    logger.error("=" * 80)
                except Exception:
                    pass  # Even logging might fail if things are really broken

                # Force immediate exit without cleanup
                # os._exit() terminates immediately without calling cleanup handlers
                # or flushing buffers - this works even when threads are stuck in I/O
                os._exit(1)


class Scheduler(Service):
    """Service to schedule jobs that match received events"""

    def __init__(self, configs, args):
        super().__init__(configs, args, args.name)
        self._api_config_yaml = yaml.dump(self._api_config)
        self._verbose = args.verbose
        self._output = args.output
        self._imgprefix = args.image_prefix or ''
        self._raw_yaml = configs.get('_raw_yaml', {})
        self._build_configs = configs.get('build_configs', {})
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        self._job_tmp_dirs = {}
        self._threads = []
        self._api_helper_lock = threading.Lock()
        self._stop_thread_lock = threading.Lock()
        self._context_lock = threading.Lock()
        self._context = {}
        self._stop_thread = False
        self._watchdog_timestamps = {}  # Thread name -> last update timestamp
        self._watchdog_lock = threading.Lock()
        # Backup is disabled by default, enable via BACKUP_FILE_LIFETIME env variable (in seconds)
        self._backup_file_lifetime = int(os.getenv('BACKUP_FILE_LIFETIME', '0'))
        self._last_backup_cleanup = 0
        if self._backup_file_lifetime > 0:
            self.log.info(f"Job backup enabled: lifetime={self._backup_file_lifetime}s, "
                          f"dir={BACKUP_DIR}")
        else:
            self.log.info("Job backup disabled (set BACKUP_FILE_LIFETIME env var to enable)")

        # Initialize KContext for runtime configuration and secrets management

        self._kcontext = kernelci.context.KContext(
            parse_cli=True,
        )

        # Initialize runtimes with KContext
        # Get runtime names from KContext (parsed from CLI --runtimes argument)
        runtime_names = self._kcontext.get_runtimes()
        runtime_types = self._kcontext.get_runtime_types()
        self.log.info(f"Runtimes from KContext: {runtime_names}")
        self.log.info(f"Runtime types from KContext: {runtime_types}")

        self.log.info(f"Initializing runtimes: {runtime_names}")

        runtimes_configs = self._get_runtimes_configs(
            configs['runtimes'], runtime_names, runtime_types
        )

        # Use the original get_all_runtimes function which properly handles user/token extraction
        # but pass kcictx for new context-aware functionality
        self._runtimes = dict(kernelci.runtime.get_all_runtimes(
            runtimes_configs, args, kcictx=self._kcontext
        ))

        # Initialize scheduler with configs and runtimes
        self._sched = kernelci.scheduler.Scheduler(configs, self._runtimes)

        # Use KContext to get default storage config
        storage_config_name = self._kcontext.get_default_storage_config()
        self.log.info(f"Default storage config from KContext: {storage_config_name}")

        if not storage_config_name:
            self.log.warning("No storage configuration found in KContext!")
            self._storage = None
            self._storage_config = None
            return

        self.log.info(f"Attempting to initialize storage config: {storage_config_name}")

        # Initialize storage using KContext
        self._storage_config = self._kcontext.get_storage_config(storage_config_name)
        self.log.info(f"KContext get_storage_config returned: {self._storage_config is not None}")

        if self._storage_config:
            self._storage = self._kcontext.init_storage(storage_config_name)
            self.log.info(f"KContext storage initialization successful: {self._storage is not None}")
        else:
            # Fallback to old method if KContext doesn't have the storage config
            self.log.info(f"KContext storage config not found, falling back to traditional method")
            try:
                self._storage_config = configs['storage_configs'][storage_config_name]

                # Get credentials from KContext for this storage config
                # Even in traditional method, use KContext for credentials
                storage_cred = self._kcontext.get_secret(f"storage.{storage_config_name}.storage_cred")
                has_cred = storage_cred is not None
                self.log.info(f"Retrieved credentials from KContext: {has_cred}")

                self._storage = kernelci.storage.get_storage(
                    self._storage_config, storage_cred
                )
                self.log.info(f"Traditional storage initialization successful: {self._storage is not None}")

            except KeyError as e:
                self.log.error(f"Storage config '{storage_config_name}' not found in configs: {e}")
                self._storage = None
                self._storage_config = None

        self._telemetry = TelemetryEmitter(self._api, 'scheduler')

    def _get_runtimes_configs(self, configs, runtimes, runtime_types=None):
        """Get runtime configurations filtered by name and/or type.

        Args:
            configs: Dictionary of all runtime configurations
            runtimes: List of runtime names to filter by (empty/None = no name filter)
            runtime_types: List of runtime types to filter by (empty/None = no type filter)

        Returns:
            Dictionary of filtered runtime configurations
        """
        runtimes_configs = {}

        # Check if both filters are provided
        if runtimes and runtime_types:
            self.log.warning(
                "Both --runtimes and --runtime-type specified. "
                "Using --runtimes (--runtime-type will be ignored)"
            )

        # Filter by runtime name if provided
        if runtimes:
            self.log.info(f"Filtering runtimes by name: {runtimes}")
            for name in runtimes:
                config = configs.get(name)
                if config:
                    runtimes_configs[name] = config
                else:
                    self.log.warning(f"Runtime '{name}' not found in configuration")
        # Otherwise filter by runtime type if provided
        elif runtime_types:
            self.log.info(f"Filtering runtimes by type: {runtime_types}")
            for name, config in configs.items():
                if config.lab_type in runtime_types:
                    runtimes_configs[name] = config

        self.log.info(f"Selected {len(runtimes_configs)} runtime(s): {list(runtimes_configs.keys())}")
        return runtimes_configs

    def _resolve_fragment_configs(self, fragment_names):
        """Resolve fragment names to their config content from raw YAML data"""
        fragments_data = self._raw_yaml.get('fragments', {})
        resolved = {}
        for name in fragment_names:
            if name.startswith('CONFIG_'):
                # Inline config option, create a pseudo-fragment
                resolved[name] = {'configs': [name]}
            elif name in fragments_data:
                resolved[name] = fragments_data[name]
            else:
                self.log.warning(f"Fragment '{name}' not found in fragments.yaml")
        return resolved

    def _cleanup_paths(self):
        job_tmp_dirs = {
            job: tmp
            for job, tmp in self._job_tmp_dirs.items()
            # ToDo: if job.is_done()
            if job.poll() is None
        }
        self._job_tmp_dirs = job_tmp_dirs
        # ToDo: if stat != 0 then report error to API?

    def _setup(self, args):
        node_sub_id = self._api.subscribe('node')
        self.log.debug(f"Node channel sub id: {node_sub_id}")
        retry_sub_id = self._api.subscribe('retry')
        self.log.debug(f"Retry channel sub id: {retry_sub_id}")
        self._context = {"node": node_sub_id, "retry": retry_sub_id}
        return {"node": node_sub_id, "retry": retry_sub_id}

    def _stop(self, context):
        self._stop_thread = True
        if hasattr(self, '_telemetry'):
            self._telemetry.close()
        for _, sub_id in self._context.items():
            if sub_id:
                self.log.info(f"Unsubscribing: {sub_id}")
                self._api_helper.unsubscribe_filters(sub_id)
        self._cleanup_paths()

    def start_health_server(self):
        """Start the basic health HTTP server"""
        health_thread = threading.Thread(
            target=run_health_server,
            daemon=True
        )
        health_thread.start()
        return health_thread

    def start_watchdog(self):
        """Start the watchdog thread to monitor scheduler threads"""
        watchdog_thread = threading.Thread(
            target=run_watchdog,
            args=(self, self.log),
            daemon=True,
            name="watchdog"
        )
        watchdog_thread.start()
        return watchdog_thread

    def backup_cleanup(self):
        """
        Cleanup the backup directory, removing files older than configured lifetime.
        Only runs if backup is enabled and at most once per hour.
        """
        if self._backup_file_lifetime <= 0:
            return

        # Only run cleanup once per hour to avoid excessive directory scans
        current_time = time.time()
        if current_time - self._last_backup_cleanup < 3600:  # 1 hour
            return

        self._last_backup_cleanup = current_time

        if not os.path.exists(BACKUP_DIR):
            return

        now = datetime.datetime.now()
        for f in os.listdir(BACKUP_DIR):
            fpath = os.path.join(BACKUP_DIR, f)
            if os.path.isfile(fpath):
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
                if (now - mtime).total_seconds() > self._backup_file_lifetime:
                    try:
                        os.remove(fpath)
                        self.log.debug(f"Removed old backup file: {fpath}")
                    except Exception as e:
                        self.log.error(f"Failed to remove backup file {fpath}: {e}")

    def backup_job(self, filename, nodeid):
        """
        Backup filename, rename to nodeid.submission and keep in BACKUP_DIR.
        Only runs if BACKUP_FILE_LIFETIME environment variable is set to a positive value.
        Periodically cleans up old backup files.
        """
        # Skip backup if disabled
        if self._backup_file_lifetime <= 0:
            return

        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)

        # Cleanup old files (this checks internally if it should run)
        self.backup_cleanup()

        # Backup file
        new_filename = os.path.join(BACKUP_DIR, f"{nodeid}.submission")
        self.log.info(f"Backing up {filename} to {new_filename}")
        try:
            shutil.copy2(filename, new_filename)
        except Exception as e:
            self.log.error(f"Failed to backup {filename} to {new_filename}: {e}")

    def _log_lava_queue_status(self, runtime, params, platform):
        if runtime.config.lab_type != 'lava':
            return
        if not hasattr(runtime, 'get_devicetype_job_count'):
            self.log.warning("LAVA runtime missing get_devicetype_job_count()")
            return

        device_type = params.get('device_type') or platform.name
        try:
            if hasattr(runtime, 'get_device_names_by_type'):
                device_names = runtime.get_device_names_by_type(
                    device_type, online_only=True
                )
                if not device_names:
                    self.log.info(
                        f"LAVA device type {device_type}: no online devices"
                    )
                    return
            queued = runtime.get_devicetype_job_count(device_type)
            self.log.info(
                "LAVA queue status: "
                f"device_type={device_type} "
                f"queued={queued}"
            )
        except Exception as exc:
            self.log.warning(
                f"Failed to query LAVA queue status for {device_type}: {exc}"
            )

    def _should_skip_due_to_queue_depth(self, runtime, job_config, platform):
        """Check if job should be skipped due to LAVA queue depth.

        Returns True if job should be skipped, False otherwise.
        """
        if runtime.config.lab_type != 'lava':
            return False

        if not hasattr(runtime, 'get_devicetype_job_count'):
            return False

        max_queue_depth = runtime.config.max_queue_depth
        device_type = job_config.params.get('device_type') if job_config.params else None
        if not device_type:
            device_type = platform.name

        try:
            if hasattr(runtime, 'get_device_names_by_type'):
                device_names = runtime.get_device_names_by_type(
                    device_type, online_only=True
                )
                if not device_names:
                    self.log.info(
                        f"Skipping job {job_config.name} for {runtime.config.name}: "
                        f"device_type={device_type} has no online devices"
                    )
                    self._telemetry.emit(
                        'job_skip',
                        runtime=runtime.config.name,
                        device_type=device_type,
                        job_name=job_config.name,
                        error_type='no_online_devices',
                        error_msg=f'device_type={device_type} '
                                  f'has no online devices',
                    )
                    return True  # Skip submission when no online devices

            queued = runtime.get_devicetype_job_count(device_type)

            if queued >= max_queue_depth:
                self.log.info(
                    f"Skipping job {job_config.name} for {runtime.config.name}: "
                    f"device_type={device_type} queue_depth={queued} >= "
                    f"max={max_queue_depth}"
                )
                self._telemetry.emit(
                    'job_skip',
                    runtime=runtime.config.name,
                    device_type=device_type,
                    job_name=job_config.name,
                    error_type='queue_depth',
                    error_msg=f'device_type={device_type} '
                              f'queue_depth={queued} >= '
                              f'max={max_queue_depth}',
                    extra={
                        'queue_depth': queued,
                        'max_depth': max_queue_depth,
                    },
                )
                return True
            return False
        except Exception as exc:
            self.log.warning(
                f"Failed to check LAVA queue depth for {device_type}: {exc}"
            )
            self._telemetry.emit(
                'runtime_error',
                runtime=runtime.config.name,
                device_type=device_type,
                job_name=job_config.name,
                error_type='online_check',
                error_msg=str(exc),
            )
            return False  # Fail-open: don't skip on errors

    def _get_tree_priority(self, tree_name, branch_name):
        for build_config in self._build_configs.values():
            if (build_config.tree.name == tree_name and
                    build_config.branch == branch_name):
                priority = build_config.priority
                if priority is not None:
                    self.log.debug(f"Tree priority for {tree_name}/{branch_name}: {priority}")
                return priority
        self.log.debug(f"No build config found for {tree_name}/{branch_name}")
        return None

    def _telemetry_fields(self, node, job_config, runtime, platform,
                          retry_counter):
        """Extract common telemetry fields from a job context."""
        kernel_rev = node.get('data', {}).get('kernel_revision', {})
        device_type = None
        if job_config.params:
            device_type = job_config.params.get('device_type')
        if not device_type:
            device_type = platform.name
        return {
            'runtime': runtime.config.name,
            'device_type': device_type,
            'job_name': job_config.name,
            'node_id': node.get('id'),
            'tree': kernel_rev.get('tree'),
            'branch': kernel_rev.get('branch'),
            'arch': job_config.params.get('arch') if job_config.params else None,
            'retry': retry_counter,
        }

    def _run_job(self, job_config, runtime, platform, input_node, retry_counter):
        try:
            node = self._api_helper.create_job_node(
                job_config,
                input_node,
                runtime=runtime,
                platform=platform,
                retry_counter=retry_counter,
            )
        except KeyError as e:
            self.log.error(' '.join([
                input_node['id'],
                runtime.config.name,
                platform.name,
                job_config.name,
                'Failed to create job node due KeyError:',
                str(e),
            ]))
            return

        if not node:
            return
        self.log.debug(f"Job node created: {node['id']}. Parent: {node['parent']}")

        kernel_rev = node['data'].get('kernel_revision', {})
        tree_name = kernel_rev.get('tree')
        branch_name = kernel_rev.get('branch')
        if tree_name and branch_name:
            tree_priority = self._get_tree_priority(tree_name, branch_name)
            if tree_priority is not None:
                node['data']['tree_priority'] = tree_priority

        # Most of the time, the artifacts we need originate from the parent
        # node. Import those into the current node, working on a copy so the
        # original node doesn't get "polluted" with useless artifacts when we
        # update it with the results
        job_node = node.copy()
        if job_node.get('parent'):
            parent_node = self._api.node.get(job_node['parent'])
            if job_node.get('artifacts'):
                job_node['artifacts'].update(parent_node['artifacts'])
            else:
                job_node['artifacts'] = parent_node['artifacts']
        if job_config.image:
            # handle it as f-string, with possible parameter imgprefix
            image_params = {
                'image_prefix': self._imgprefix
            }
            imagename = job_config.image.format_map(image_params)
            job_config.image = imagename
        job = kernelci.runtime.Job(job_node, job_config)
        job.platform_config = platform
        job.storage_config = self._storage_config
        params = runtime.get_params(job, self._api.config)
        if not params:
            self.log.error(' '.join([
                node['id'],
                runtime.config.name,
                platform.name,
                job_config.name,
                "Invalid job parameters, aborting...",
            ]))
            node['state'] = 'done'
            node['result'] = 'incomplete'
            node['data']['error_code'] = 'invalid_job_params'
            self._telemetry.emit(
                'runtime_error',
                error_type='invalid_job_params',
                error_msg='Invalid job parameters, aborting...',
                **self._telemetry_fields(
                    node, job_config, runtime, platform, retry_counter
                ),
            )
            try:
                self._api.node.update(node)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)
            return
        # Process potential f-strings in `params` with configured job params
        # and platform attributes
        kernel_revision = job_node['data']['kernel_revision']['version']
        extra_args = {
            'krev': f"{kernel_revision['version']}.{kernel_revision['patchlevel']}"
        }
        extra_args.update(job.config.params)
        params = job.platform_config.format_params(params, extra_args)
        # Resolve fragment configs for kbuild jobs
        if 'fragments' in params and params['fragments']:
            fragment_configs = self._resolve_fragment_configs(params['fragments'])
            params['fragment_configs'] = fragment_configs
        # we experience sometimes that the job is not created properly
        # due exception in the runtime.generate method
        try:
            data = runtime.generate(job, params)
        except Exception as e:
            self.log.error(' '.join([
                node['id'],
                runtime.config.name,
                platform.name,
                job_config.name,
                'Failed to generate job data:',
                str(e),
            ]))
            node['state'] = 'done'
            node['result'] = 'incomplete'
            node['data']['error_code'] = 'job_generation_error'
            node['data']['error_msg'] = str(e)
            self._telemetry.emit(
                'runtime_error',
                error_type='job_generation_error',
                error_msg=str(e),
                **self._telemetry_fields(
                    node, job_config, runtime, platform, retry_counter
                ),
            )
            try:
                self._api.node.update(node)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)
            return

        if not data:
            self.log.error(' '.join([
                node['id'],
                runtime.config.name,
                platform.name,
                job_config.name,
                "Failed to generate job definition, aborting...",
            ]))
            node['state'] = 'done'
            node['result'] = 'fail'
            node['data']['error_code'] = 'job_generation_error'
            self._telemetry.emit(
                'runtime_error',
                error_type='job_generation_error',
                error_msg='Failed to generate job definition, '
                          'aborting...',
                **self._telemetry_fields(
                    node, job_config, runtime, platform, retry_counter
                ),
            )
            try:
                self._api.node.update(node)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)
            return
        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = runtime.save_file(data, tmp.name, params)
        self.backup_job(output_file, node['id'])
        self._log_lava_queue_status(runtime, params, platform)
        try:
            running_job = runtime.submit(output_file)
        except Exception as e:
            self.log.error(' '.join([
                node['id'],
                runtime.config.name,
                platform.name,
                job_config.name,
                'submit error:',
                str(e),
            ]))
            node['state'] = 'done'
            node['result'] = 'incomplete'
            node['data']['error_code'] = 'submit_error'
            node['data']['error_msg'] = str(e)
            self._telemetry.emit(
                'runtime_error',
                error_type='submit_error',
                error_msg=str(e),
                **self._telemetry_fields(
                    node, job_config, runtime, platform, retry_counter
                ),
            )
            try:
                self._api.node.update(node)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)
            return

        # If submit() returned None, this might be pull-based job
        # (e.g. LAVA) where we cannot get a job ID at submission time
        # but job retriever on lab side will set the job ID later
        job_id = None
        if running_job:
            job_id = str(runtime.get_job_id(running_job))
            node['data']['job_id'] = job_id
        else:
            # This is "pull-based" job, so we likely have artifact
            # for job definition
            artifact_url = runtime.get_job_definition_url()
            self.log.debug(f"Job definition URL: {artifact_url}")
            if artifact_url:
                # node['artifacts'] is a dict of name:url
                if node.get('artifacts'):
                    node['artifacts']['job_definition'] = artifact_url
                else:
                    node['artifacts'] = {'job_definition': artifact_url}

        if platform.name == "kubernetes":
            context = runtime.get_context()
            node['data']['job_context'] = context

        try:
            self._api.node.update(node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

        log_parts = [
            node['id'],
            runtime.config.name,
            platform.name,
            job_config.name,
        ]
        if job_id is not None:
            log_parts.append(job_id)

        self.log.info(' '.join(log_parts))

        tfields = self._telemetry_fields(
            node, job_config, runtime, platform, retry_counter
        )
        self._telemetry.emit(
            'job_submission',
            job_id=job_id,
            **tfields,
        )

        if runtime.config.lab_type in ['shell', 'docker']:
            self._job_tmp_dirs[running_job] = tmp

    def translate_freq(self, freq):
        """
        Translate the frequency to seconds
        Format is: [Nd][Nh][Nm], where each field optional
        """
        freq_sec = 0
        freq_re = re.compile(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?')
        freq_match = freq_re.match(freq)
        if freq_match:
            days, hours, minutes = freq_match.groups()
            if days:
                freq_sec += int(days) * 24 * 60 * 60
            if hours:
                freq_sec += int(hours) * 60 * 60
            if minutes:
                freq_sec += int(minutes) * 60

        return freq_sec

    def _search_job_freq(self, jobname, tree, branch, tstamp, platform):
        """
        Search for jobs with the same name, tree, branch and created
        timestamp greater than tstamp
        """
        attributes = {
            'name': jobname,
            'data.kernel_revision.tree': tree,
            'data.kernel_revision.branch': branch,
            'data.platform': platform.name,
            'created__gte': tstamp,
        }
        nodes = self._api.node.find(attributes, 0, 1)
        return nodes

    def _verify_frequency(self, job, node, platform):
        """Verify if the job can be run, as frequency limit
        how often it can be run for particular tree/branch
        """
        try:
            tree = node['data']['kernel_revision']['tree']
            branch = node['data']['kernel_revision']['branch']
            frequency = job.params['frequency']
        except KeyError:
            print(f"Job {job.name} does not have valid frequency parameters")
            return True

        freq_sec = self.translate_freq(frequency)
        if not freq_sec or freq_sec < 60:
            print(f"Job {job.name} has invalid frequency parameter {frequency}")
            return True
        # date format 2024-10-08T18:56:07.810000, ISO 8601?
        now = datetime.datetime.now()
        tstamp = now - datetime.timedelta(seconds=freq_sec)
        if self._search_job_freq(job.name, tree, branch, tstamp, platform):
            print(f"Job {job.name} for tree {tree} branch {branch} "
                  f"created less than {freq_sec} seconds ago"
                  f", skipping due frequency limit {frequency}")
            return False
        return True

    def _verify_architecture_filter(self, job, node):
        """Verify if the job can be run, if node has architecture filter
        """
        if job.kind == 'kbuild' and 'architecture_filter' in node['data'] and \
           node['data']['architecture_filter'] and 'arch' in job.params and \
           job.params['arch'] not in node['data']['architecture_filter']:
            msg = f"Node {node['id']} has architecture filter "
            msg += f"{node['data']['architecture_filter']} "
            msg += f"job {job.name} is kbuild and arch {job.params['arch']}"
            print(msg)
            return False
        return True

    def _run(self, context):
        for channel, sub_id in self._context.items():
            thread = threading.Thread(
                target=self._run_scheduler,
                args=(channel, sub_id,),
                name=f"scheduler-{channel}"
            )
            self._threads.append(thread)
            thread.start()

        for thread in self._threads:
            thread.join()

    def _run_scheduler(self, channel, sub_id):
        self.log.info("Listening for available checkout events")
        self.log.info("Press Ctrl-C to stop.")
        subscribe_retries = 0

        while True:
            # Update timestamp for watchdog
            thread_name = threading.current_thread().name
            with self._watchdog_lock:
                self._watchdog_timestamps[thread_name] = time.time()

            with self._stop_thread_lock:
                if self._stop_thread:
                    break
            event = None
            try:
                event = self._api_helper.receive_event_data(sub_id, block=False)
                if not event:
                    # If we received a keep-alive event, just continue
                    continue
            except Exception as e:
                with self._stop_thread_lock:
                    if self._stop_thread:
                        break
                self.log.error(f"Error receiving event: {e}")
                self.log.debug(f"Re-subscribing to channel: {channel}")
                sub_id = self._api.subscribe(channel)
                with self._context_lock:
                    self._context[channel] = sub_id
                subscribe_retries += 1
                if subscribe_retries > 3:
                    self.log.error(f"Failed to re-subscribe to channel: {channel}")
                    return False
                continue
            subscribe_retries = 0
            for job, runtime, platform, rules in self._sched.get_schedule(event):
                input_node = self._api.node.get(event['id'])
                jobfilter = event.get('jobfilter')
                # Add to node data the jobfilter if it exists in event
                if jobfilter and isinstance(jobfilter, list):
                    input_node['jobfilter'] = jobfilter
                platform_filter = event.get('platform_filter')
                if platform_filter and isinstance(platform_filter, list):
                    input_node['platform_filter'] = platform_filter
                # we cannot use rules, as we need to have info about job too
                if job.params.get('frequency', None):
                    if not self._verify_frequency(job, input_node, platform):
                        continue
                if not self._verify_architecture_filter(job, input_node):
                    continue
                with self._api_helper_lock:
                    flag = self._api_helper.should_create_node(rules, input_node)
                if flag:
                    # Check LAVA queue depth before creating job node
                    if self._should_skip_due_to_queue_depth(runtime, job, platform):
                        continue
                    retry_counter = event.get('retry_counter', 0)
                    self._run_job(job, runtime, platform, input_node, retry_counter)

        return True


class cmd_loop(Command):
    help = "Listen to pub/sub events and run in a loop"
    args = [Args.api_config, Args.output]
    opt_args = [
        Args.verbose,
        {
            'name': '--runtimes',
            'nargs': '*',
            'help': "Runtime environments to use, all by default",
        },
        {
            'name': '--runtime-type',
            'nargs': '*',
            'help': "Runtime types to use (lava, kubernetes, docker, shell, pull_labs)",
        },
        {
            'name': '--name',
            'help': "Service name used to create log file",
            'required': True
        },
    ]

    def __call__(self, configs, args):
        scheduler = Scheduler(configs, args)

        # Start health server
        health_thread = scheduler.start_health_server()

        # Start watchdog to monitor scheduler threads
        watchdog_thread = scheduler.start_watchdog()

        return scheduler.run()


if __name__ == '__main__':
    opts = parse_opts('scheduler', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    # Also load raw YAML data to access fragments
    raw_yaml_data = kernelci.config.load_yaml(yaml_configs)
    configs['_raw_yaml'] = raw_yaml_data
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
