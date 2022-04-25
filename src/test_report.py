#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Jeny Sadadia
# Author: Jeny Sadadia <jeny.sadadia@gmail.com>
#
# Copyright (C) 2022 Collabora Limited
# Author: Alexandra Pereira <alexandra.pereira@collabora.com>

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

    def create_email(self, template):
        if template:
            email_msg = email.mime.text.MIMEText(template, "plain", "utf-8")
            email_msg['To'] = os.getenv('EMAIL_TO')
            email_msg['From'] = os.getenv('EMAIL_FROM')
            email_msg['Subject'] = os.getenv('EMAIL_SUBJECT')

            if(os.getenv('SERVER_USER') and os.getenv('SERVER_PASSWORD')):
                SERVER_USER = os.getenv('SERVER_USER')
                SERVER_PASSWORD = os.getenv('SERVER_PASSWORD')
                EMAIL_HOST = os.getenv('EMAIL_HOST')
                self._print(email_msg)
                email_server = smtplib.SMTP(EMAIL_HOST)
                email_server.starttls()
                email_server.login(SERVER_USER, SERVER_PASSWORD)
                email_server.send_message(email_msg)
                email_server.quit()
                self._print('E-mail sent!')
            else:
                self._print('Missing e-mail credentials.')


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
                templateEnv = jinja2.Environment(
                            loader=jinja2.FileSystemLoader("./config/reports/")
                        )
                template = templateEnv.get_template("test-report.jinja2")
                logger.log_message(logging.INFO,
                                   template.render(total_runs=1,
                                                   total_failures=0,
                                                   root=root_node,
                                                   tests=[node]))
                email_msg = template.render(total_runs=1, total_failures=0,
                                            root=root_node, tests=[node])
                self.create_email(email_msg)

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
