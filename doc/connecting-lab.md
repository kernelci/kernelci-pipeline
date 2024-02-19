---
title: "Connecting LAVA Lab to the pipeline instance"
date: 2024-02-19
description: "Connecting a LAVA lab to the KernelCI pipeline"
weight: 2
---

As we are moving towards the new KernelCI API and pipeline, we need to make sure
all the existing LAVA labs are connected to the new pipeline instance.  This
document explains how to do this.

## Token setup

The first step is to generate a token for the lab. This is done by the lab admin,
and the token is used to submit jobs from pipeline to the lab, and to authenticate
LAVA lab callbacks to the pipeline.
Requirements for the token:
- Description: a string matching the regular expression `[a-zA-Z0-9\-]+`, for example "kernelci-new-api-callback"
- Value: arbitrary, kept secret

*IMPORTANT!* You need to have both fields, as way how LAVA works:
- You submit the job with the token-description in job definition
- LAVA lab sends the result back to the pipeline with the token-value (retrieved by that token-description) in the header

More details in [LAVA documentation](https://docs.lavasoftware.org/lava/user-notifications.html#notification-callbacks).


## Pipeline configuration

### Secrets (toml) file

The next step is to add the token to the pipeline services configuration files.
Secrets are stored in the `kernelci.secrets` section of the `kernelci.toml` file,
and added manually by the KernelCI system administrators.  For example, the token
can be added to the `runtime.lava-labname` section of the `kernelci.toml` file:

```toml
[runtime.<lava-labname>]
runtime_token="lab-token-value"
```
Section name `lava-labname` should be replaced with the actual lab name, **matching the name of the lab in the pipeline configuration**.
lab-token-value should be replaced with the actual token value. Usually it is a long string of random characters.
For example in our documentation we use `lava-broonie` as the lab name, so the section will look like this
```toml
[runtime.lava-broonie]
runtime_token="M5d34U5pwCJnQLun"
```

### docker-compose file

You need to add lab name to the scheduler-lava service in the `docker-compose.yml` file.
For example, the following configuration adds the `lava-broonie` lab to the scheduler-lava service:

```yml
scheduler-lava:
    <<: *scheduler
    container_name: 'kernelci-pipeline-scheduler-lava'
    command:
      - './pipeline/scheduler.py'
      - '--settings=${KCI_SETTINGS:-/home/kernelci/config/kernelci.toml}'
      - 'loop'
      - '--runtimes'
      - 'lava-collabora'
      - 'lava-collabora-staging'
      - 'lava-broonie'
```


### yaml pull request

The final step is to submit a pull request to the `kernelci-pipeline` repository
to add the lab configuration to the yaml file.
For example, see the following [pull request](https://github.com/kernelci/kernelci-pipeline/pull/426).

In details the pull request should add a new entry to the `runtimes` section of the configuration file:

```yaml

  lava-broonie:
    lab_type: lava
    url: 'https://lava.sirena.org.uk/'
    priority_min: 10
    priority_max: 40
    notify:
      callback:
        token: kernelci-new-api-callback
        url: https://staging.kernelci.org:9100

```
Where `lava-broonie` is the name of the lab, `lava` is the type of the lab, `url` is the URL of the lab, `priority_min` and `priority_max` are the priority range allowed to jobs, assigned by lab owner, and `notify` is the notification configuration for the lab.  The `callback` section contains the token description (name) and the URL of the pipeline instance LAVA callback endpoint.
More details how LAVA callback and token works can be found in the [LAVA documentation](https://docs.lavasoftware.org/lava/user-notifications.html#notification-callbacks).

### Jobs and devices specific to the lab

For testing it is better to add separate job and platforms specific to the lab.  For example, the following yaml file adds a job and a device type for the `lava-broonie` lab:

```yaml
jobs:
  baseline-arm64-broonie: *baseline-job

platforms:

  sun50i-h5-libretech-all-h3-cc:
    <<: *arm64-device
    mach: allwinner
    dtb: dtbs/allwinner/sun50i-h5-libretech-all-h3-cc.dtb

scheduler:

  - job: baseline-arm64-broonie
    event:
      channel: node
      name: kbuild-gcc-10-arm64
      result: pass
    runtime:
      type: lava
      name: lava-broonie
    platforms:
      - sun50i-h5-libretech-all-h3-cc
```

job usually define tasks to be run, for example kernel build, or in our context it will be test running on particular device (platform).  The device is defined in the `platforms` section, and the job is defined in the `jobs` section.  Conditions for the job to be run are defined in the `scheduler` section.
More details about pipeline configuration can be found in the pipeline configuration documentation (TBD).
