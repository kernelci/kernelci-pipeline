#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2025 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import sys
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service


class JobRetry(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'job_retry')

    def _setup(self, args):
        return self._api_helper.subscribe_filters({
            "state": "done",
            "result": "incomplete",
            "kind": ("kbuild", "job"),
        })

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)
        sys.stdout.flush()

    def _find_parent_kind(self, node, api_helper, kind):
        parent_id = node.get('parent')
        if not parent_id:
            return None
        parent_node = api_helper.api.node.get(parent_id)
        if not parent_node:
            return None
        if parent_node.get('kind') == kind:
            return parent_node
        return self._find_parent_kind(parent_node, api_helper, kind)

    def _run(self, sub_id):
        self.log.info("Job retry: Listening for events... ")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            try:
                node, _ = self._api_helper.receive_event_node(sub_id)
                self.log.debug(f"Event received: {node['id']}")
            except Exception as e:
                self.log.error(f"Error receiving event: {e}")
                continue

            # Check retry count before submitting a retry
            retry_counter = node.get("retry_counter", 0)
            self.log.debug(f"{node['id']}: Node current retry_counter: {retry_counter}")
            if retry_counter >= 3:
                self.log.info(f"{node['id']} Job has already retried 3 times. \
Not submitting a retry.")
                continue

            parent_kind = None
            if node.get("kind") == "job":
                parent_kind = "kbuild"
            if node.get("kind") == "kbuild":
                parent_kind = "checkout"
            if parent_kind:
                event_data = self._find_parent_kind(node, self._api_helper, parent_kind)
                if not event_data:
                    self.log.error(f"Not able to find parent node for {node['id']}")
                    continue
                if node["kind"] == "kbuild":
                    event_data["jobfilter"] = [f'{node["name"]}+']
                else:
                    event_data["jobfilter"] = [node["name"]]
                # Change event data state to available to trigger jobs based on scheduler configs
                event_data["state"] = "available"
                if node["kind"] == "job":
                    event_data["platform_filter"] = [node["data"].get("platform")]
                event_data["retry_counter"] = retry_counter + 1
                event_data["debug"] = {"retry_by": str(node["id"])}
                self.log.debug(f"{node['id']}:Event data retry_counter: {event_data['retry_counter']}")
                event = {'data': event_data}
                self._api_helper.api.send_event('retry', event)
                self.log.info(f"Job retry for node {node['id']} submitted. Parent node: {event_data['id']}")
                self.log.debug(f"Event:{event}")
            else:
                self.log.error(f"Not able to retry the job as parent kind is unknown: {node['id']}")
        return True


class cmd_run(Command):
    help = "Retry failed/incomplete builds and tests"
    args = [Args.api_config]

    def __call__(self, configs, args):
        return JobRetry(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('job_retry', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    configs = kernelci.config.load(yaml_configs)
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
