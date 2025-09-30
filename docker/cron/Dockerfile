# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2025 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

FROM python:3.10

RUN apt-get update && apt-get install -y cron

WORKDIR /home/kernelci

# Install kci-dev package
ARG kci_dev_url=https://github.com/kernelci/kci-dev.git
ARG kci_dev_rev=main
RUN pip install git+$kci_dev_url@$kci_dev_rev

# Copy cron job file
COPY maestro-validate-cron-job.txt /etc/cron.d/maestro-validate-cron-job
RUN chmod 0644 /etc/cron.d/maestro-validate-cron-job
# Register the cron job
RUN crontab /etc/cron.d/maestro-validate-cron-job

CMD ["cron", "-f"]
