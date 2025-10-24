#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2025 Jeny Sadadia
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

"""SMTP Email Sender module"""

from email.mime.multipart import MIMEMultipart
import email
import email.mime.text
import os
import smtplib
import sys
from urllib.parse import urljoin
import jinja2
import toml
import kernelci.config
import kernelci.storage


SETTINGS = toml.load(os.getenv('KCI_SETTINGS', '/home/kernelci/config/kernelci.toml'))
CONFIGS = kernelci.config.load(
    SETTINGS.get('DEFAULT', {}).get('yaml_config', 'config')
)

class EmailSender:
    """Class to send email report using SMTP"""
    def __init__(self, smtp_host, smtp_port, email_sender, email_recipient, email_password):
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._email_sender = email_sender
        self._email_recipient = email_recipient
        self._email_pass = email_password
        template_env = jinja2.Environment(
                            loader=jinja2.FileSystemLoader(".")
                        )
        self._template = template_env.get_template("validation_report_template.jinja2")

    def _smtp_connect(self):
        """Method to create a connection with SMTP server"""
        if self._smtp_port == 465:
            smtp = smtplib.SMTP_SSL(self._smtp_host, self._smtp_port)
        else:
            smtp = smtplib.SMTP(self._smtp_host, self._smtp_port)
            smtp.starttls()
        smtp.login(self._email_sender, self._email_pass)
        return smtp

    def _create_email(self, email_subject, email_content):
        """Method to create an email message from email subject, contect,
        sender, and receiver"""
        email_msg = MIMEMultipart()
        email_text = email.mime.text.MIMEText(email_content, "plain", "utf-8")
        email_text.replace_header('Content-Transfer-Encoding', 'quopri')
        email_text.set_payload(email_content, 'utf-8')
        email_msg.attach(email_text)
        if isinstance(self._email_recipient, list):
            email_msg['To'] = ','.join(self._email_recipient)
        else:
            email_msg['To'] = self._email_recipient
        email_msg['From'] = self._email_sender
        email_msg['Subject'] = email_subject
        return email_msg

    def _send_email(self, email_msg):
        """Method to send an email message using SMTP"""
        smtp = self._smtp_connect()
        if smtp:
            smtp.send_message(email_msg)
            smtp.quit()

    def _get_report(self, report_location, report_url):
        try:
            with open(report_location, 'r', encoding='utf-8') as f:
                report_content = f.read()
            content = self._template.render(
                report_content=report_content, report_url=report_url
            )
        except Exception as e:
            print(f"Error reading report file: {e}")
            sys.exit()
        return content

    def create_and_send_email(self, email_subject, report_location, report_url):
        """Method to create and send email"""
        email_content = self._get_report(report_location, report_url)
        email_msg = self._create_email(
            email_subject, email_content
        )
        self._send_email(email_msg)

def get_storage(storage_config_name):
    storage_config = CONFIGS['storage_configs'][storage_config_name]
    storage_cred = SETTINGS['storage'][storage_config_name]['storage_cred']
    return kernelci.storage.get_storage(storage_config, storage_cred)

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Command line argument missing. Specify report filename.")
        sys.exit()

    report_filename = sys.argv[1]    
    storage_config_name = SETTINGS.get('DEFAULT', {}).get('storage_config')
    storage = get_storage(storage_config_name)
    email_sender_configs = SETTINGS.get('cron', {})
    file_path = email_sender_configs.get('file_path')
    storage_url = storage.config.base_url
    upload_path = email_sender_configs.get('upload_path')
    email_sender = email_sender_configs.get('email_sender')
    email_recipient = email_sender_configs.get('email_recipient')
    email_password = email_sender_configs.get('email_password')
    smtp_host = email_sender_configs.get('smtp_host')
    smtp_port = email_sender_configs.get('smtp_port')

    if not any([file_path, storage_url, upload_path,
                email_sender, email_recipient, smtp_host, smtp_port, email_password]):
        print("Missing config variables")
        sys.exit()

    report_url = f"{storage_url+upload_path+'/'+report_filename}"
    email_sender = EmailSender(
        smtp_host=smtp_host, smtp_port=smtp_port,
        email_sender=email_sender,
        email_recipient=email_recipient,
        email_password=email_password,
    )
    try:
        subject = "Maestro Validation report"
        email_sender.create_and_send_email(
            email_subject=subject,
            report_location=urljoin(file_path, report_filename),
            report_url=report_url,
        )
    except Exception as err:
        print(err)
