---
title: "Connecting LAVA Lab to the pipeline instance"
date: 2024-05-29
description: "Connecting a LAVA lab to the KernelCI pipeline"
weight: 3
---

As we are moving towards the new KernelCI API and pipeline, we need to make sure
all the existing LAVA labs are connected to the new pipeline instance.  This
document explains how to do this.

## Token setup

The first step is to generate a token for the lab. This is done by the lab admin,
and the token is used to submit jobs from pipeline to the lab and to authenticate
LAVA lab callbacks to the pipeline.

Requirements for the token:
- `Description`: a string matching the regular expression `[a-zA-Z0-9\-]+`, for example "kernelci-new-api-callback"
- `Value`: arbitrary, kept secret

*IMPORTANT!* You need to have both fields, as that's how LAVA works:
- You submit the job with the token description in job definition
- LAVA lab sends the result back to the pipeline with the token value (retrieved by that token-description) in the header

More details in [LAVA documentation](https://docs.lavasoftware.org/lava/user-notifications.html#notification-callbacks).


## Pipeline configuration

### Update pipeline configuration

The first step is to add the lab configuration to [pipeline configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file.

Please add a new entry to the `runtimes` section of the configuration file
as follows:

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
Where `lava-broonie` is the name of the lab, `lab_type` indicates the lab is of a `lava` type, `url` is the URL of the lab, `priority_min` and `priority_max` are the priority range allowed to jobs, assigned by lab owner, and `notify` is the notification configuration for the lab.  The `callback` section contains the token description that you received from the above [step](#token-setup) and the URL of the pipeline instance LAVA callback endpoint.
More details on how LAVA callback and token works can be found in the [LAVA documentation](https://docs.lavasoftware.org/lava/user-notifications.html#notification-callbacks).

Please submit a pull request to [`kernelci-pipeline`](https://github.com/kernelci/kernelci-pipeline) repository to add the lab configurations. See the
[pull request](https://github.com/kernelci/kernelci-pipeline/pull/426) for reference.

### KernelCI configuration (TOML) file

The next step is to add the token to the pipeline services configuration file i.e. [`config/kernelci.toml`](https://github.com/kernelci/kernelci-pipeline/blob/main/config/kernelci.toml) file. Every lab/runtime should have a section `runtime.<lab-name>` in the TOML file. The lab token should be stored in a key named `runtime_token` inside the section.
For example,

```toml
[runtime.<lab-name>]
runtime_token="<lab-token-value>"
```

Section name `lab-name` should be replaced with the actual lab name, **matching the name of the lab in the pipeline configuration i.e. `config/pipeline.yaml`**.
`lab-token-value` should be replaced with the actual token value that you received in the [`Token setup`](#token-setup) step. Usually, it is a long string of random characters.
For example, in our documentation we used `lava-broonie` as the lab name, so the section will look like this:
```toml
[runtime.lava-broonie]
runtime_token="N0tAS3creTT0k3n"
```

### `docker-compose` file

We are running all the pipeline services as docker containers.
You need to provide lab name to `--runtimes` argument to the [`scheduler-lava`](https://github.com/kernelci/kernelci-pipeline/blob/main/docker-compose.yaml#L80)
service in the `docker-compose.yml` file to enable the lab.
For example, the following configuration adds the `lava-broonie` lab along with other labs:

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

### Jobs and devices specific to the lab

The last step is to add some jobs that you want KernelCI to submit to the lab.
You also need to add platforms that the job will run on in the lab.
For example, the following adds a job and a device type for the `lava-broonie` lab:

```yaml
jobs:
  baseline-arm64-broonie:
    template: baseline.jinja2
    kind: test

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

Jobs usually define tasks to be run such as kernel build or a test suite running on a particular device (platform).
The device is defined in the `platforms` section, and the job is defined in the `jobs` section.  Conditions for the job to be run are defined in the `scheduler` section.
More details about pipeline configuration can be found in the pipeline configuration documentation (TBD).

> **Note** We have [`lava-callback`](https://github.com/kernelci/kernelci-pipeline/blob/main/docker-compose-lava.yaml#L10) service that will receive job results from the lab and send them to the API.

And, here you go! You have successfully connected your lab with KernelCI.
