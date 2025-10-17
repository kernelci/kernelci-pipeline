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

import kernelci
import kernelci.config
import kernelci.runtime
import kernelci.scheduler
import kernelci.storage
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service

BACKUP_DIR = '/tmp/kci-backup'
BACKUP_FILE_LIFETIME = 24 * 60 * 60  # 24 hours in seconds


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


class Scheduler(Service):
    """Service to schedule jobs that match received events"""

    def __init__(self, configs, args):
        super().__init__(configs, args, args.name)
        self._api_config_yaml = yaml.dump(self._api_config)
        self._verbose = args.verbose
        self._output = args.output
        self._imgprefix = args.image_prefix or ''
        if not os.path.exists(self._output):
            os.makedirs(self._output)
        rconfigs = (
            configs['runtimes'] if args.runtimes is None
            else self._get_runtimes_configs(configs['runtimes'], args.runtimes)
        )
        runtimes = dict(kernelci.runtime.get_all_runtimes(rconfigs, args))
        self._sched = kernelci.scheduler.Scheduler(configs, runtimes)
        self._storage_config = configs['storage_configs'][args.storage_config]
        storage_cred = os.getenv('KCI_STORAGE_CREDENTIALS')
        self._storage = kernelci.storage.get_storage(
            self._storage_config, storage_cred
        )
        self._job_tmp_dirs = {}
        self._threads = []
        self._api_helper_lock = threading.Lock()
        self._stop_thread_lock = threading.Lock()
        self._context_lock = threading.Lock()
        self._context = {}
        self._stop_thread = False

    def _get_runtimes_configs(self, configs, runtimes):
        runtimes_configs = {}
        for name in runtimes:
            config = configs.get(name)
            if config:
                runtimes_configs[name] = config
        return runtimes_configs

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

    def backup_cleanup(self):
        """
        Cleanup the backup directory, removing files older than 24h
        """
        if not os.path.exists(BACKUP_DIR):
            return
        now = datetime.datetime.now()
        for f in os.listdir(BACKUP_DIR):
            fpath = os.path.join(BACKUP_DIR, f)
            if os.path.isfile(fpath):
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath))
                if (now - mtime).total_seconds() > BACKUP_FILE_LIFETIME:
                    os.remove(fpath)

    def backup_job(self, filename, nodeid):
        """
        Backup filename, rename to nodeid.submission and keep in BACKUP_DIR
        Also check if BACKUP_DIR have files older than 24h, delete them
        """
        if not os.path.exists(BACKUP_DIR):
            os.makedirs(BACKUP_DIR)
        # cleanup old files
        self.backup_cleanup()
        # backup file
        new_filename = os.path.join(BACKUP_DIR, f"{nodeid}.submission")
        self.log.info(f"Backing up {filename} to {new_filename}")
        # copy file to backup directory
        try:
            shutil.copy2(filename, new_filename)
        except Exception as e:
            self.log.error(f"Failed to backup {filename} to {new_filename}: {e}")

    def _run_job(self, job_config, runtime, platform, input_node, retry_counter):
        try:
            node = self._api_helper.create_job_node(job_config,
                                                    input_node,
                                                    runtime, platform, retry_counter)
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
            try:
                self._api.node.update(node)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)
            return
        tmp = tempfile.TemporaryDirectory(dir=self._output)
        output_file = runtime.save_file(data, tmp.name, params)
        self.backup_job(output_file, node['id'])
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
            try:
                self._api.node.update(node)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)
            return

        job_id = str(runtime.get_job_id(running_job))
        node['data']['job_id'] = job_id

        if platform.name == "kubernetes":
            context = runtime.get_context()
            node['data']['job_context'] = context

        try:
            self._api.node.update(node)
        except requests.exceptions.HTTPError as err:
            err_msg = json.loads(err.response.content).get("detail", [])
            self.log.error(err_msg)

        self.log.info(' '.join([
            node['id'],
            runtime.config.name,
            platform.name,
            job_config.name,
            job_id,
        ]))
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
            'name': '--name',
            'help': "Service name used to create log file",
            'required': True
        },
    ]

    def __call__(self, configs, args):
        scheduler = Scheduler(configs, args)

        # Start health server with scheduler instance access
        health_thread = scheduler.start_health_server()

        return scheduler.run()


if __name__ == '__main__':
    opts = parse_opts('scheduler', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
