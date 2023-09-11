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
from base import Service


class TestReport(Service):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'test_report')
        self._email_sender = EmailSender(
            args.smtp_host, args.smtp_port,
            email_sender=args.email_sender,
            email_recipient=args.email_recipient,
        ) if args.smtp_host and args.smtp_port else None

    def _dump_report(self, content):
        print(content, flush=True)

    def _get_group_stats(self, parent_id):
        return {
            'total': self._api.count_nodes({"parent": parent_id}),
            'failures': self._api.count_nodes({
                "parent": parent_id,
                "result": "fail"
            })
        }

    def _get_group_data(self, root_node):
        group = root_node['group']
        revision = root_node['revision']
        # Get count of group nodes and exclude the root node from it
        group_nodes = self._api.count_nodes({
            'revision.commit': revision['commit'],
            'revision.tree': revision['tree'],
            'revision.branch': revision['branch'],
            'group': group,
        })-1
        failures = self._api.get_nodes({
            'revision.commit': revision['commit'],
            'revision.tree': revision['tree'],
            'revision.branch': revision['branch'],
            'group': group,
            'result': 'fail',
        })
        failures = [
            node for node in failures if node['id'] != root_node['id']
        ]
        parent_path_len = len(root_node['path'])
        for node in failures:
            node['path'] = '.'.join(node['path'][parent_path_len:])
        return {'root': root_node, 'nodes': group_nodes, 'failures': failures}

    def _get_results_data(self, root_node):
        group_nodes = self._api.get_nodes({"parent": root_node['id']})
        groups = {
            node['name']: self._get_group_data(node)
            for node in group_nodes
        }
        group_stats = self._get_group_stats(root_node['id'])
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
        results = self._get_results_data(root_node)
        stats = results['stats']
        groups = results['groups']
        subject = f"\
[STAGING] {revision['tree']}/{revision['branch']} {revision['describe']}: \
{stats['total']} runs {stats['failures']} failures"
        content = template.render(
            subject=subject, root=root_node, groups=groups
        )
        return content, subject

    def _send_report(self, subject, content):
        if self._email_sender:
            self._email_sender.create_and_send_email(subject, content)
        else:
            self.log.info("No SMTP settings provided, not sending email")


class TestReportLoop(TestReport):
    """Command to send reports upon receiving events in a loop"""

    def _setup(self, args):
        return self._api_helper.subscribe_filters({
            'name': 'checkout',
            'state': 'done',
        })

    def _stop(self, sub_id):
        if sub_id:
            self._api_helper.unsubscribe_filters(sub_id)

    def _run(self, sub_id):
        self.log.info("Listening for completed nodes")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            root_node = self._api_helper.receive_event_node(sub_id)
            content, subject = self._get_report(root_node)
            self._dump_report(content)
            self._send_report(subject, content)

        return True


class TestReportSingle(TestReport):
    """Command to send a report for a single root node"""

    def _setup(self, args):
        return {
            'root_node': self._api.get_node(args.node_id),
            'dump': args.dump,
            'send': args.send,
        }

    def _run(self, ctx):
        content, subject = self._get_report(ctx['root_node'])
        if ctx['dump']:
            self._dump_report(content)
        if ctx['send']:
            self._send_report(subject, content)
        return True


class cmd_loop(Command):
    help = "Generate test report"
    args = [
        Args.api_config,
    ]
    opt_args = [
        {
            'name': '--smtp-host',
            'help': "SMTP server host name.  If omitted, emails won't be sent",
        },
        {
            'name': '--smtp-port',
            'help': "SMTP server port number",
            'type': int,
        },
        {
            'name': '--email-sender',
            'help': "Email address of test report sender",
        },
        {
            'name': '--email-recipient',
            'help': "Email address of test report recipient",
        },
    ]

    def __call__(self, configs, args):
        return TestReportLoop(configs, args).run(args)


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
        return TestReportSingle(configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('test_report', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
