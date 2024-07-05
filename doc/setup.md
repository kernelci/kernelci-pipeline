---
title: "Pipeline setup"
date: 2024-07-05
description: "KernelCI Pipeline self-hosted setup"
weight: 2
---

GitHub repository:
[`kernelci-pipeline`](https://github.com/kernelci/kernelci-pipeline.git)

The KernelCI Pipeline is a set of tools and scripts to automate the
testing of Linux kernels on real hardware. In some cases, like
development, or running your own compiling farm and lab, you may want
to run the KernelCI Pipeline on your own infrastructure. This document
describes how to set up the KernelCI Pipeline on your own server.

## Requirements

KernelCI Pipeline is a set of Python scripts that can be run on any
Linux system. We have two ways to run the KernelCI Pipeline:

- **Docker**: The KernelCI Pipeline can be run in a Docker container, using
    the docker-compose tool to manage the containers.
    Suitable for staging and development environments.

- **Kubernetes**: The KernelCI Pipeline can be run in a Kubernetes cluster.
    At current time, the Kubernetes setup is tuned to Azure (AKS).
    Some deployment scripts are available in the kernelci-deploy repository.
    Suitable for production environments.

## Configuration files

The KernelCI Pipeline uses a set of environment variables and configuration
files to define the behavior of the pipeline. You can find configuration
files in the `config/` directory of the repository.
Most of the yaml files in the `config/` directory are used to define the
configuration of the KernelCI Pipeline. Separate files, such as loggers.conf,
are used to define the logging configuration of the pipeline. TOML file
used to define secrets and other configuration that should not be shared.

## Environment variables

The KernelCI Pipeline uses a set of environment variables to define the
behavior of the pipeline. Here is a list of the environment variables with
their default values and a brief description of their purpose:

- `KCI_API_TOKEN`: Mandatory. API token to authenticate with the KernelCI API.
- `KCI_SETTINGS`: Location of TOML file with secrets and other configuration.
- `KCI_INSTANCE`: Name of the instance (might appear in job names and etc).
- `KCI_INSTANCE_CALLBACK`: Callback URL for the instance (LAVA, etc).
- `EMAIL_USER`: Email address to send notifications from.
- `EMAIL_PASSWORD`: Password for the email address.

### KCIDB bridge specific environment variables

- `KCIDB_PROJECT_ID`: KCIDB project ID.
- `KCIDB_TOPIC_NAME`: KCIDB topic name. 
- `GOOGLE_APPLICATION_CREDENTIALS`: Path to the Google Cloud credentials file (json)

For more details please contact the KCIDB team.



