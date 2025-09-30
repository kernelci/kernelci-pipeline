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


class EmailSender:
    """Class to send email report using SMTP"""
    def __init__(self, smtp_host, smtp_port, email_sender, email_recipient):
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._email_sender = email_sender
        self._email_recipient = email_recipient
        self._email_pass = os.getenv('EMAIL_PASSWORD')

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

    def create_and_send_email(self, email_subject, email_content):
        """Method to create and send email"""
        email_msg = self._create_email(
            email_subject, email_content
        )
        self._send_email(email_msg)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Command line argument missing. Specify report filename.")
        sys.exit()

    report_filename = sys.argv[1]
    storage_url = os.getenv('STORAGE_URL')
    upload_path = os.getenv('UPLOAD_PATH')
    email_sender = os.getenv('EMAIL_SENDER')
    email_recipient = os.getenv('EMAIL_RECIPIENT')
    smtp_host = os.getenv('SMTP_HOST')
    smtp_port = os.getenv('SMTP_PORT')

    if not any([storage_url, upload_path, email_sender, email_recipient, smtp_host, smtp_port]):
        print("Missing environment variables")
        sys.exit()

    report_url = f"{storage_url+upload_path+'/'+report_filename}"
    email_sender = EmailSender(
        smtp_host=smtp_port, smtp_port=smtp_host,
        email_sender=email_sender,
        email_recipient=email_recipient,
    )
    try:
        subject = "Maestro Validation report"
        content = f"Hi,\n\nHere is the link to the validation report:\n{report_url}\n\nThanks,\nKernelCI team"
        email_sender.create_and_send_email(
                subject, content
            )
    except Exception as err:
        print(err)
