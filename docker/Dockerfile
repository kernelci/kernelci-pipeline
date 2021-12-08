# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

FROM python:3.10
MAINTAINER "KernelCI TSC" <kernelci-tsc@groups.io>

# Upgrade pip3 - never mind the warning about running this as root
RUN pip3 install --upgrade pip

RUN useradd kernelci -u 1000 -d /home/kernelci -s /bin/bash
RUN mkdir -p /home/kernelci
RUN chown kernelci: /home/kernelci
USER kernelci
ENV PATH=$PATH:/home/kernelci/.local/bin
WORKDIR /home/kernelci

# Install kernelci-core from api-pipeline topic branch
RUN pip install git+https://github.com/kernelci/kernelci-core.git@api-pipeline
