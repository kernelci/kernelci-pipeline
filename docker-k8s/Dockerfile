# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2021, 2022 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

FROM kernelci/staging-kernelci
MAINTAINER "KernelCI TSC" <kernelci-tsc@groups.io>

ARG DEBIAN_FRONTEND=noninteractive

USER root

# SSL / HTTPS support
RUN apt-get update && apt-get install --no-install-recommends -y \
    apt-transport-https \
    ca-certificates

# pip3
RUN apt-get update && apt-get install --no-install-recommends -y \
    python3-pip
RUN pip3 install --upgrade pip


# -----------------------------------------------------------------------------
# Kubernetes
#

RUN python3 -m pip install --upgrade kubernetes


# -----------------------------------------------------------------------------
# gcloud-sdk
# https://cloud.google.com/sdk/docs/downloads-apt-get
#

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg

RUN echo "\
deb [signed-by=/usr/share/keyrings/cloud.google.gpg] \
https://packages.cloud.google.com/apt cloud-sdk main" | \
    tee -a /etc/apt/sources.list.d/google-cloud-sdk.list

RUN curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | \
    apt-key --keyring /usr/share/keyrings/cloud.google.gpg add -

RUN apt-get update && apt-get install -y \
    google-cloud-sdk \
    kubectl


# -----------------------------------------------------------------------------
# Azure CLI
# https://docs.microsoft.com/en-us/cli/azure/install-azure-cli-apt
#

RUN curl -sL https://packages.microsoft.com/keys/microsoft.asc | \
    gpg --dearmor |  tee /etc/apt/trusted.gpg.d/microsoft.asc.gpg > /dev/null

RUN AZ_REPO=bullseye && \
    echo "\
deb [arch=amd64] https://packages.microsoft.com/repos/azure-cli/ \
$AZ_REPO main" | \
    tee /etc/apt/sources.list.d/azure-cli.list

RUN apt-get update && apt-get install -y \
    azure-cli


# -----------------------------------------------------------------------------
# Switch back to kernelci user
#

USER kernelci
