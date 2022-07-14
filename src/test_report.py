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
        self._email_host = args.email_host
        self._email_port = int(args.email_port)
        self._email_send_from = 'bot@kernelci.org'
        self._email_subject = 'Kernel CI Test Reports'
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
            if self._email_port == 465:
                email_server = smtplib.SMTP_SSL(self._email_host,
                                                self._email_port)
            else:
                email_server = smtplib.SMTP(self._email_host,
                                            self._email_port)
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
                                     f"Recipents or Sender refused: {error}.")
        except (smtplib.SMTPException) as error:
            self._logger.log_message(logging.ERROR,
                                     f"Generic error sending email: {error}.")
        else:
            self._logger.log_message(logging.INFO, "Email Sent.")

    def run(self):
        sub_id = self._db.subscribe('node')
        self._logger.log_message(logging.INFO,
                                 "Listening for test completion events")
        self._logger.log_message(logging.INFO, "Press Ctrl-C to stop.")

        try:
            while True:
                sys.stdout.flush()
                event = self._db.get_event(sub_id)

                node = self._db.get_node_from_event(event)
                if node['status'] == 'pending':
                    continue

                root_node = self._db.get_root_node(node['_id'])
                template_env = jinja2.Environment(
                            loader=jinja2.FileSystemLoader("./config/reports/")
                        )
                template = template_env.get_template("test-report.jinja2")
                email_content = template.render(total_runs=1, total_failures=0,
                                                root=root_node, tests=[node])
                email_msg = self.create_email(email_content,
                                              self._email_send_to,
                                              self._email_send_from,
                                              self._email_subject)
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
    args = [Args.db_config]

    def __call__(self, configs, args):
        generate_test_report = TestReport(configs, args)
        generate_test_report.run()


if __name__ == '__main__':
    opts = parse_opts('test_report', globals())
    configs = kernelci.config.load('config/pipeline.yaml')
    status = opts.command(configs, opts)
    sys.exit(0 if status is True else 1)
