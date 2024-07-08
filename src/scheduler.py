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

import kernelci
import kernelci.config
import kernelci.runtime
import kernelci.scheduler
import kernelci.storage
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service


class Scheduler(Service):
    """Service to schedule jobs that match received events"""

    def __init__(self, configs, args):
        super().__init__(configs, args, 'scheduler')
        self._api_config_yaml = yaml.dump(self._api_config)
        self._verbose = args.verbose
        self._output = args.output
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
        return self._api.subscribe('node')

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)
        self._cleanup_paths()

    def _run_job(self, job_config, runtime, platform, input_node):
        node = self._api_helper.create_job_node(job_config, input_node,
                                                runtime, platform)
        if not node:
            return
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
        data = runtime.generate(job, params)
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
        try:
            running_job = runtime.submit(output_file)
        except Exception as e:
            self.log.error(' '.join([
                node['id'],
                runtime.config.name,
                platform.name,
                job_config.name,
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

    def _run(self, sub_id):
        self.log.info("Listening for available checkout events")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            event = self._api_helper.receive_event_data(sub_id)
            for job, runtime, platform, rules in self._sched.get_schedule(event):
                input_node = self._api.node.get(event['id'])
                jobfilter = event.get('jobfilter')
                # Add to node data the jobfilter if it exists in event
                if jobfilter and isinstance(jobfilter, list):
                    input_node['jobfilter'] = jobfilter
                if self._api_helper.should_create_node(rules, input_node):
                    self._run_job(job, runtime, platform, input_node)

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
    ]

    def __call__(self, configs, args):
        return Scheduler(configs, args).run()


if __name__ == '__main__':
    opts = parse_opts('scheduler', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
