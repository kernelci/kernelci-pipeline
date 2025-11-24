---
title: "Connecting and Running Pull Labs runtime"
date: 2025-02-14
description: "Connecting a PULL_LABS compatible lab to the KernelCI pipeline"
weight: 4
---

KernelCI supports labs that follow the [PULL_LABS protocol](https://github.com/kernelci/kernelci-core/pull/3008) in addition to
LAVA- and Kubernetes-based integrations. This guide shows the minimum
configuration needed to make a lab instance visible to the pipeline.

There is an payload script in `tools/example_pull_lab.py` to which
provides a simply way to execute these pull-lab payloads.

The examples below mirror the demo entries committed in this repository.
Replace the names and tokens with the values that match your deployment.

## Pipeline configuration

Add a new runtime entry to [`config/pipeline.yaml`](../config/pipeline.yaml)
under the `runtimes` section:

```yaml
  pull-labs-demo:
    lab_type: pull_labs
    poll_interval: 45
    timeout: 7200
    storage_name: docker-host
    notify:
      callback:
        token: kernelci-pull-labs-demo
```

- `poll_interval` controls how often the lab polls the API for new jobs.
- `timeout` sets the default job timeout in seconds that will be written
  into the generated pull-labs job definition.
- `storage_name` must point to a storage backend defined in the same file.
- `notify.callback.token` is the name advertised to the lab so it knows
  which callback token value to present when sending back results.

## Scheduler configuration

Declare at least one scheduler entry in
[`config/scheduler.yaml`](../config/scheduler.yaml) that targets the runtime:

```yaml
  - job: baseline-arm-pull-labs-demo
    event:
      channel: node
      kind: kbuild
      name: kbuild-gcc-12-arm
      state: available
    runtime:
      type: pull_labs
      name: pull-labs-demo
    platforms:
      - beaglebone-black
      - imx6dl-udoo
```

The scheduler entry must reference a job defined in `config/jobs.yaml` and
point to a set of existing platforms.

## Jobs

Add or reuse a job definition inside [`config/jobs.yaml`](../config/jobs.yaml):

```yaml
jobs:
  baseline-arm-pull-labs-demo: *baseline-job
```

Any template that depends on the runtime name (for example `baseline.jinja2`)
will automatically extend `config/runtime/base/pull_labs.jinja2` from
`kernelci-core` once the scheduler selects a `pull_labs` runtime.

## Runtime secrets

When the lab requires a secret token for callbacks, store the
token value in [`config/kernelci.toml`](../config/kernelci.toml) by adding:

```toml
[runtime.pull-labs-demo]
runtime_token="REPLACE_WITH_CALLBACK_TOKEN_VALUE"
```

The name inside the square brackets must match the runtime name from
`config/pipeline.yaml`.

## Deployment

Ensure the `scheduler` service is started with the `--runtimes pull-labs-demo`
argument in the relevant `docker-compose` file so the new runtime becomes active.
The lab will see the generated events once it authenticates with the callback
token value paired with the token name defined in the pipeline configuration.

## Running the Example Pull Lab Script

The `tools/example_pull_lab.py` script provides a simple way to execute pull-lab
payloads using tuxrun for QEMU-based virtual targets.

### Prerequisites

Tuxrun is required to run the jobs, Tuxrun requires podman also to be setup to
execute the jobs.

- Install tuxrun: `pip install tuxrun`
- Install podman: `sudo apt install podman`
- Tuxrun handles downloads and QEMU VM execution automatically

### Running the Script

```bash
python tools/example_pull_lab.py
```

The script will:
- Detect architecture from job definitions
- Support filtering by platform, group, device, and runtime
- Use `--cache-dir` for storing caches and outputs in `./tuxrun-cache/`
- Saves output to timestamped directories in `./test_output/`

**TODO:** Support for FVP (Fixed Virtual Platform) and DUT (Device Under Test)
jobs will be added in future versions, along with publishing to KCIDB.
