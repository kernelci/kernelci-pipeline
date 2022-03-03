#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Maryam Yusuf
# Author: Maryam Yusuf <maryam.m.yusuf1802@gmail.com>
import datetime
import os
import sys

import kernelci
import kernelci.data
from kernelci.config import load
from kernelci.cli import Args, Command, parse_opts
from kcidb import Client
import kcidb


class cmd_run(Command):
    help = "Listen for events and send them to KCDIB"
    args = [Args.db_config]

    def __call__(self, configs, args):
        db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        db = kernelci.data.get_db(db_config, api_token)

        if not os.getenv('GOOGLE_APPLICATION_CREDENTIALS'):
            print("No GOOGLE_APPLICATION_CREDENTIALS environment variable")
            return False

        topic_name = os.getenv('KCIDB_TOPIC_NAME')
        if not topic_name:
            print("No KCIDB_TOPIC_NAME environment variable")
            return False

        project_id = os.getenv('KCIDB_PROJECT_ID')
        if not project_id:
            print("No KCIDB_PROJECT_ID environment variable")
            return False

        client = Client(project_id=project_id, topic_name=topic_name)
        if client is None:
            print("Failed to create client connection to KCIDB")
            return False

        sub_id = db.subscribe('node')
        print("Listening for events... ")
        print("Press Ctrl-C to stop.")
        sys.stdout.flush()

        tz_utc = datetime.timezone(datetime.timedelta(hours=0))

        try:
            while True:
                event = db.get_event(sub_id)
                node = db.get_node_from_event(event)
                if node['name'] != 'checkout' or node['status'] != 'pass':
                    continue

                print(f"Submitting node to KCIDB: {node['_id']}")
                sys.stdout.flush()

                created_time = datetime.datetime.fromisoformat(node["created"])
                if not created_time.tzinfo:
                    created_time = datetime.datetime.fromtimestamp(
                        created_time.timestamp(), tz=tz_utc)
                revision = {
                    "builds": [],
                    "checkouts": [{
                        "id": f"kernelci:{node['_id']}",
                        "origin": "kernelci",
                        "tree_name": node["revision"]["tree"],
                        "git_repository_url": node["revision"]["url"],
                        "git_commit_hash": node["revision"]["commit"],
                        "git_repository_branch": node["revision"]["branch"],
                        "start_time": created_time.isoformat(),
                        "patchset_hash": "",
                    }],
                    "tests": [],
                    "version": {
                        "major": 4,
                        "minor": 0
                    }
                }
                self.send_revision(client, revision)

            sys.stdout.flush()

        except KeyboardInterrupt:
            print("Stopping.")
        finally:
            db.unsubscribe(sub_id)

        sys.stdout.flush()

        return True

    def send_revision(self, client, revision):
        if self.validate_revision(revision):
            return client.submit(revision)
        print(f"Aborting, invalid data")
        sys.stdout.flush()

    @staticmethod
    def validate_revision(revision):
        return kcidb.io.SCHEMA.is_valid(revision)


if __name__ == '__main__':
    opts = parse_opts('runner', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
