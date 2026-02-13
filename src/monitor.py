#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import datetime
import sys
import time
import kernelci
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service
from telemetry import TelemetryEmitter


class Monitor(Service):
    LOG_FMT = ("{time:26s}  {kind:15s} {commit:12s}  {id:24s} "
               "{state:9s}  {result:8s}  {name}")

    def __init__(self, configs, args):
        super().__init__(configs, args, 'monitor')
        self._log_titles = self.LOG_FMT.format(
            time="Time", kind="Kind", commit="Commit", id="Node Id",
            state="State", result="Result", name="Name")
        self._telemetry = TelemetryEmitter(self._api, 'monitor')

    def _setup(self, args):
        return self._api.subscribe('node')

    def _stop(self, sub_id):
        if sub_id:
            self._api.unsubscribe(sub_id)
        self._telemetry.close()
        sys.stdout.flush()

    def _run(self, sub_id):
        state_map = {
            "running": "Running",
            "available": "Available",
            "closing": "Closing",
            "done": "Done",
        }

        result_map = {
            "pass": "Pass",
            "fail": "Fail",
            "skip": "Skipped",
            "incomplete": "Incomplete",
            None: "-",
        }

        self.log.info("Monitor: Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")
        print(self._log_titles, flush=True)
        subscribe_retries = 0
        while True:
            event = None
            try:
                event = self._api.receive_event(sub_id)
            except Exception as e:
                self.log.error(f"Error receiving event: {e}, re-subscribing in 10 seconds")
                time.sleep(10)
                sub_id = self._api.subscribe('node')
                subscribe_retries += 1
                if subscribe_retries > 3:
                    self.log.error("Failed to re-subscribe to node events")
                    return False
                continue
            subscribe_retries = 0
            obj = event.data
            dt = datetime.datetime.fromisoformat(event['time'])
            try:
                commit = obj['data']['kernel_revision']['commit'][:12]
            except (KeyError, TypeError):
                commit = str(None)
            result = result_map[obj['result']] if obj['result'] else str(None)
            print(self.LOG_FMT.format(
                time=dt.strftime('%Y-%m-%d %H:%M:%S.%f'),
                kind=obj['kind'],
                commit=commit,
                id=obj['id'],
                state=state_map[obj['state']],
                result=result,
                name=obj['name']
            ), flush=True)
            self._emit_node_telemetry(obj)

        return True

    def _emit_node_telemetry(self, obj):
        """Emit telemetry for nodes that reached done state.

        Non-LAVA nodes are always emitted here. LAVA nodes are normally
        handled by the lava-callback service, but timed-out LAVA nodes
        never receive a callback, so we emit them here instead.
        """
        kind = obj.get('kind')
        state = obj.get('state')
        if state != 'done' or kind not in ('kbuild', 'job', 'test'):
            return

        data = obj.get('data', {})
        runtime = data.get('runtime', '')
        error_code = data.get('error_code', '')

        # Skip LAVA nodes that got a normal callback — lava_callback
        # service already emits richer telemetry for those.  Only let
        # timed-out LAVA nodes through (error_code == 'node_timeout').
        # kbuild nodes never go through LAVA callback, so always emit.
        if (kind != 'kbuild'
                and runtime.startswith('lava-')
                and error_code != 'node_timeout'):
            return

        kernel_rev = data.get('kernel_revision', {})
        common = {
            'runtime': runtime,
            'device_type': data.get('platform', ''),
            'job_name': obj.get('name', ''),
            'node_id': obj.get('id'),
            'tree': kernel_rev.get('tree'),
            'branch': kernel_rev.get('branch'),
            'arch': data.get('arch'),
            'result': obj.get('result'),
        }

        if error_code == 'node_timeout':
            common['error_type'] = 'node_timeout'
            common['error_msg'] = data.get('error_msg', 'Node timed-out')

        kind_map = {
            'kbuild': 'build_result',
            'job': 'job_result',
            'test': 'test_result',
        }
        event_kind = kind_map[kind]
        if kind == 'test':
            common['test_name'] = obj.get('name', '')
        self._telemetry.emit(event_kind, **common)


class cmd_run(Command):
    help = "Listen for events and report them on stdout"
    args = [Args.api_config]

    def __call__(self, configs, args):
        return Monitor(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('monitor', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
