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
        self._logger.log_message(logging.INFO, email_content)
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

    def _get_test_analysis(self, nodes):
        total_runs = len(nodes)
        total_failures = sum(node['status'] == "fail" for node in nodes)
        return total_runs, total_failures

    def _create_test_report(self, root_node, child_nodes):
        total_runs, total_failures = self._get_test_analysis(child_nodes)

        template_env = jinja2.Environment(
                            loader=jinja2.FileSystemLoader("./config/reports/")
                        )
        template = template_env.get_template("test-report.jinja2")
        subject_str = (f"{root_node['revision']['tree']}/\
{root_node['revision']['branch']} \
{root_node['revision']['describe']}: \
{total_runs} runs {total_failures} fails")
        email_content = template.render(subject_str=subject_str,
                                        root=root_node,
                                        tests=child_nodes)
        return email_content, subject_str

    def run(self):
        sub_id = self._db.subscribe_node_channel(filters={
            'name': 'tarball',
            'status': 'complete',
        })

        self._logger.log_message(logging.INFO,
                                 "Listening for complete tarballs")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                root_node = self._db.receive_node(sub_id)
                child_nodes = (self._db.get_nodes({
                    "parent": root_node['_id']
                })).get('items')
                content, subject = self._create_test_report(
                    root_node, child_nodes
                )
                print(content, flush=True)
                smtp = self._smtp_connect()
                if smtp:
                    email_msg = self._create_email(
                        self._email_send_to, self._email_send_from,
                        subject, content
                    )
                    smtp.send_message(email_msg)
                    smtp.quit()
        except KeyboardInterrupt:
            self._logger.log_message(logging.INFO, "Stopping.")
        except Exception:
            self._logger.log_message(logging.ERROR, traceback.format_exc())
        finally:
            self._db.unsubscribe(sub_id)


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
        generate_test_report.run()


if __name__ == '__main__':
    opts = parse_opts('test_report', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
