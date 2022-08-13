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

    def create_email(self, email_content, send_to, send_from, email_subject):
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

    def smtp_connect(self, email_user, email_password):
        try:
            if self._smtp_port == 465:
                email_server = smtplib.SMTP_SSL(self._smtp_host,
                                                self._smtp_port)
            else:
                email_server = smtplib.SMTP(self._smtp_host,
                                            self._smtp_port)
                email_server.starttls()
            email_server.login(email_user, email_password)
        except (smtplib.SMTPAuthenticationError,
                smtplib.SMTPConnectError,
                smtplib.SMTPServerDisconnected) as error:
            self._logger.log_message(logging.ERROR,
                                     f"Connect or auth error: {error}.")
        except (smtplib.SMTPHeloError,
                smtplib.SMTPDataError,
                smtplib.SMTPNotSupportedError,
                RuntimeError) as error:
            self._logger.log_message(logging.ERROR,
                                     f"STMP server error: {error}.")
        except (smtplib.SMTPException) as error:
            self._logger.log_message(logging.ERROR,
                                     f"Generic error connecting: {error}.")
        else:
            self._logger.log_message(logging.DEBUG, "Server Connected.")
            return email_server

    def send_mail(self, email_msg, email_server):
        try:
            email_server.send_message(email_msg)
        except (smtplib.SMTPRecipientsRefused,
                smtplib.SMTPSenderRefused) as error:
            self._logger.log_message(logging.ERROR,
                                     f"Recipients or Sender refused: {error}.")
        except (smtplib.SMTPException) as error:
            self._logger.log_message(logging.ERROR,
                                     f"Generic error sending email: {error}.")
        else:
            self._logger.log_message(logging.INFO, "Email Sent.")

    def get_test_analysis(self, nodes):
        total_runs = len(nodes)
        total_failures = sum(node['status'] == "fail" for node in nodes)
        return total_runs, total_failures

    def create_test_report(self, root_node, child_nodes):
        total_runs, total_failures = self.get_test_analysis(child_nodes)

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
                child_nodes = self._db.get_nodes({"parent": root_node['_id']})

                email_content, email_sub = self.create_test_report(
                    root_node, child_nodes
                )
                email_msg = self.create_email(email_content,
                                              self._email_send_to,
                                              self._email_send_from,
                                              email_sub)
                email_server = self.smtp_connect(self._email_user,
                                                 self._email_pass)
                if email_server:
                    self.send_mail(email_msg, email_server)
                    email_server.quit()
        except KeyboardInterrupt as e:
            self._logger.log_message(logging.INFO, "Stopping.")
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
