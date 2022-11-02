#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Jeny Sadadia
# Author: Jeny Sadadia <jeny.sadadia@gmail.com>
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>
# Author: Alexandra Pereira <alexandra.pereira@collabora.com>
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

import logging
import os
import sys
import traceback

import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts
import jinja2

from kernelci_pipeline.email_sender import EmailSender
from logger import Logger


class TestReport:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._logger = Logger("config/logger.conf", "test_report")
        self._email_sender = EmailSender(
            args.smtp_host, args.smtp_port,
            email_send_from='bot@kernelci.org',
            email_send_to='kernelci-results-staging@groups.io',
        )

    def _dump_report(self, content):
        print(content, flush=True)

    def _get_group_stats(self, parent_id):
        return {
            'total': self._db.count_nodes({"parent": parent_id}),
            'failures': self._db.count_nodes({
                "parent": parent_id,
                "result": "fail"
            })
        }

    def _get_group_data(self, root_node):
        group = root_node['group']
        revision = root_node['revision']
        # Get count of group nodes and exclude the root node from it
        group_nodes = self._db.count_nodes({
            'revision.commit': revision['commit'],
            'revision.tree': revision['tree'],
            'revision.branch': revision['branch'],
            'group': group,
        })-1
        failures = self._db.get_nodes({
            'revision.commit': revision['commit'],
            'revision.tree': revision['tree'],
            'revision.branch': revision['branch'],
            'group': group,
            'result': 'fail',
        })
        failures = [
            node for node in failures if node['_id'] != root_node['_id']
        ]
        parent_path_len = len(root_node['path'])
        for node in failures:
            node['path'] = '.'.join(node['path'][parent_path_len:])
        return {'root': root_node, 'nodes': group_nodes, 'failures': failures}

    def _get_results_data(self, root_node):
        group_nodes = self._db.get_nodes({"parent": root_node['_id']})
        groups = {
            node['name']: self._get_group_data(node)
            for node in group_nodes
        }
        group_stats = self._get_group_stats(root_node['_id'])
        return {
            'stats': group_stats,
            'groups': groups,
        }

    def _get_report(self, root_node):
        template_env = jinja2.Environment(
                            loader=jinja2.FileSystemLoader("./config/reports/")
                        )
        template = template_env.get_template("test-report.jinja2")
        revision = root_node['revision']
        if root_node['result'] == 'incomplete':
            subject = (f"\
{revision['tree']}/{revision['branch']} {revision['describe']}: \
Failed to create source tarball for {root_node['name']}")
            content = template.render(
                subject=subject, root=root_node, groups={}
            )
        else:
            results = self._get_results_data(root_node)
            stats = results['stats']
            groups = results['groups']
            subject = (f"\
{revision['tree']}/{revision['branch']} {revision['describe']}: \
{stats['total']} runs {stats['failures']} failures")
            content = template.render(
                subject=subject, root=root_node, groups=groups
            )
        return content, subject

    def _send_report(self, subject, content):
        try:
            self._email_sender.create_and_send_email(
                    subject, content
                )
        except Exception as err:
            self._logger.log_message(logging.ERROR, err)

    def loop(self):
        """Method to execute in a loop"""
        sub_id = self._db.subscribe_node_channel(filters={
            'name': 'checkout',
            'state': 'done',
        })

        self._logger.log_message(logging.INFO, "Listening for completed nodes")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                root_node = self._db.receive_node(sub_id)
                content, subject = self._get_report(root_node)
                self._dump_report(content)
                self._send_report(subject, content)
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())
        finally:
            self._db.unsubscribe(sub_id)

    def run(self, node_id, dump, send):
        """Method to execute for a single node"""
        root_node = self._db.get_node(node_id)
        content, subject = self._get_report(root_node)
        if dump:
            self._dump_report(content)
        if send:
            self._send_report(subject, content)


class cmd_loop(Command):
    help = "Generate test report"
    args = [
        Args.db_config,
        {
            'name': '--smtp-host',
            'help': "SMTP server host name",
        },
        {
            'name': '--smtp-port',
            'help': "SMTP server port number",
            'type': int,
            'default': 25,
        },
    ]

    def __call__(self, configs, args):
        test_report = TestReport(configs, args)
        test_report.loop()


class cmd_run(Command):
    help = "Generate single test report for a given checkout node id"
    args = cmd_loop.args + [
        {
            'name': '--node-id',
            'help': "id of the checkout node rather than pub/sub",
        }
    ]
    opt_args = [
        {
            'name': '--dump',
            'action': 'store_true',
            'help': "Dump the report on stdout",
        },
        {
            'name': '--send',
            'action': 'store_true',
            'help': "Send the email over SMTP",
        },
    ]

    def __call__(self, configs, args):
        test_report = TestReport(configs, args)
        test_report.run(args.node_id, args.dump, args.send)


if __name__ == '__main__':
    opts = parse_opts('test_report', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
