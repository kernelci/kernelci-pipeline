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

from email.mime.multipart import MIMEMultipart
import logging
import email
import email.mime.text
import os
import smtplib
import sys
import traceback

import kernelci.config
import kernelci.db
from kernelci.cli import Args, Command, parse_opts
import jinja2

from logger import Logger


class TestReport:

    def __init__(self, configs, args):
        self._db_config = configs['db_configs'][args.db_config]
        api_token = os.getenv('API_TOKEN')
        self._db = kernelci.db.get_db(self._db_config, api_token)
        self._logger = Logger("config/logger.conf", "test_report")
        self._smtp_host = args.smtp_host
        self._smtp_port = args.smtp_port
        self._email_send_from = 'bot@kernelci.org'
        self._email_send_to = 'kernelci-results-staging@groups.io'
        self._email_user = os.getenv('EMAIL_USER')
        self._email_pass = os.getenv('EMAIL_PASSWORD')

    def _create_email(self, send_to, send_from, email_subject, email_content):
        email_msg = MIMEMultipart()
        email_text = email.mime.text.MIMEText(email_content, "plain", "utf-8")
        email_text.replace_header('Content-Transfer-Encoding', 'quopri')
        email_text.set_payload(email_content, 'utf-8')
        email_msg.attach(email_text)
        if isinstance(send_to, list):
            email_msg['To'] = ','.join(send_to)
        else:
            email_msg['To'] = send_to
        email_msg['From'] = send_from
        email_msg['Subject'] = email_subject
        return email_msg

    def _smtp_connect(self):
        try:
            if self._smtp_port == 465:
                smtp = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port)
            else:
                smtp = smtplib.SMTP(self._smtp_host, self._smtp_port)
                smtp.starttls()
            smtp.login(self._email_user, self._email_pass)
            return smtp
        except Exception as e:
            self._logger.log_message(
                logging.ERROR, f"Failed to connect to SMTP server: {e}"
            )
            return None

    def _get_group_stats(self, group_nodes):
        return {
            'total': len(group_nodes),
            'failures': sum(node['result'] == 'fail' for node in group_nodes)
        }

    def _get_group_data(self, root_node):
        group = root_node['group']
        revision = root_node['revision']
        group_nodes = self._db.get_nodes({
            'revision.commit': revision['commit'],
            'revision.tree': revision['tree'],
            'revision.branch': revision['branch'],
            'group': group,
        })
        group_nodes = [
            node for node in group_nodes if node['_id'] != root_node['_id']
        ]
        failures = [
            node for node in group_nodes if node['result'] == 'fail'
        ]
        parent_path_len = len(root_node['path'])
        for node in group_nodes:
            node['path'] = '.'.join(node['path'][parent_path_len:])
        return {'root': root_node, 'nodes': group_nodes, 'failures': failures}

    def _get_results_data(self, root_node):
        group_nodes = self._db.get_nodes({"parent": root_node['_id']})
        group_stats = self._get_group_stats(group_nodes)
        groups = {
            node['name']: self._get_group_data(node)
            for node in group_nodes
        }
        return {
            'stats': group_stats,
            'groups': groups,
        }

    def _create_test_report(self, root_node):
        results = self._get_results_data(root_node)
        template_env = jinja2.Environment(
                            loader=jinja2.FileSystemLoader("./config/reports/")
                        )
        template = template_env.get_template("test-report.jinja2")
        revision = root_node['revision']
        stats = results['stats']
        groups = results['groups']
        subject = (f"\
{revision['tree']}/{revision['branch']} {revision['describe']}: \
{stats['total']} runs {stats['failures']} failures")
        content = template.render(
            subject=subject, root=root_node, groups=groups
        )
        return content, subject

    def _send_email(self, content, subject):
        smtp = self._smtp_connect()
        if smtp:
            email_msg = self._create_email(
                self._email_send_to, self._email_send_from,
                subject, content
            )
            smtp.send_message(email_msg)
            smtp.quit()

    def _make_report(self, root_node, dump=True, send=False):
        content, subject = self._create_test_report(root_node)
        if dump:
            print(content, flush=True)
        if send:
            self._send_email(content, subject)

    def run_loop(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'name': 'checkout',
            'state': 'done',
        })

        self._logger.log_message(logging.INFO, "Listening for complete jobs")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                root_node = self._db.receive_node(sub_id)
                self._make_report(root_node, dump=True, send=True)
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())
        finally:
            self._db.unsubscribe(sub_id)

    def run_from_id(self, node_id, dump, send):
        root_node = self._db.get_node(node_id)
        self._make_report(root_node, dump, send)


class cmd_run(Command):
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
        generate_test_report = TestReport(configs, args)
        generate_test_report.run_loop()


class cmd_single(Command):
    help = "Generate single test report for a given checkout node id"
    args = cmd_run.args + [
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
        generate_test_report = TestReport(configs, args)
        generate_test_report.run_from_id(args.node_id, args.dump, args.send)


if __name__ == '__main__':
    opts = parse_opts('test_report', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
