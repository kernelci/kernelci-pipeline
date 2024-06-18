---
title: "Developer Documentation"
date: 2024-06-18
description: "KernelCI Pipeline developer manual"
weight: 4
---

## Enabling a new Kernel tree

We can monitor different kernel trees in KernelCI.
This manual describes how to enable them in [`kernelci-pipeline`](https://github.com/kernelci/kernelci-pipeline.git).


### Pipeline configuration
The pipeline [configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file has `trees` section.
In order to enable a new tree, we need to add an entry there.

```yaml
trees:
    <tree-name>:
        url: "<tree-url>"
```
For example,
```yaml
trees:
    kernelci:
        url: "https://github.com/kernelci/linux.git"
```

After adding a `trees` entry, we need to define build configurations for it.
In the same [configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file, `build_configs` section is there to specify them.
For example, we need to specify which branch to monitor of a particular tree and other build variants as well.

For instance,
```yaml
build_configs:

    kernelci_staging-mainline:
        tree: kernelci
        branch: 'staging-mainline'
        variants:
            gcc-10:
                build_environment: gcc-10
                architectures:
                    x86_64:
                        base_defconfig: 'x86_64_defconfig'
                        filters:
                            - regex: { defconfig: 'x86_64_defconfig' }
```

That's it! The tree is enabled now.
All the jobs defined under `jobs` section of [config file](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) would run on the newly added tree until specified otherwise.


## Enabling a new test

KernelCI currently runs several test suites. This manual is intended
to provide documentation for developers on how to enable new tests.


### Job definition
The pipeline [configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file has `jobs` section for defining various build and test jobs. The currently enabled tests are `kver`, `baseline`, `kunit`, `kselftest`, and `sleep` tests.
`ChromeOS` specific job definitions are located in [config/jobs-chromeos.yaml](https://github.com/kernelci/kernelci-pipeline/blob/main/config/jobs-chromeos.yaml) file.

To enable a new test, an entry needs to be added as follows:

```yaml
jobs:
  <job-name>:
    template: <job-template>
    kind: <kind of job, either 'kbuild', 'job' or 'test'>
    image: <docker-image-name>
    params: <parameters such as arch, compiler, defconfig>
    rules: <job-rules>
    kcidb_test_suite: <kcidb-mapping>
```

Here is the description of each field:
- A `jinja2` template should be added to [`config/runtime`](https://github.com/kernelci/kernelci-pipeline/tree/main/config/runtime) directory.
The template will be used to generate test definition.
- `kind` field denotes type of the job. It should be `kbuild` for build jobs, `job` for a test suite, and simply `test` for a single test case.
- `image` field specifies the Docker image used for building and running the test. Please note that this field is optional. Some jobs such as LAVA test jobs use image defined in the test definition template instead.
- `params` includes parameters used for building the kernel (for `kbuild` job) or running the test, such as architecture, compiler, defconfig option, job timeout, etc.
- `rules` will define job rules. If the test should be scheduled for specific kernel tree, branch, or version, these rules can be defined in this section.
- `kcidb_test_suite` is a field used to map KernelCI test suite name with
KCIDB test. Build jobs (with `kind: kbuild`) do not require this field.

Please have a look at [config/pipeline.yaml](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) and [config/jobs-chromeos.yaml](https://github.com/kernelci/kernelci-pipeline/blob/main/config/jobs-chromeos.yaml) files to check currently added job definitions for reference.


### Schedule the job

We also need a `scheduler` entry for the newly added job to specify pre-conditions for scheduling and define runtime and platforms for job submissions.

For example,
```yaml
scheduler:
    - job: <job-name>
      event: <API-pubsub-event>
      runtime:
        type: <runtime-type>
        name: <runtime-name>
      platforms:
        - <device-type>
```

Here is the description of each field:
- `job` field specifies the job name, which must match the name used in the jobs section
- `event` specifies the API PubSub event triggering the test scheduling.
For example, if the test requires a build job such as `kbuild-gcc-10-arm64`
to be successfully completed, specify the event as follows:
```
event:
    channel: node
    name: kbuild-gcc-10-arm64
    result: pass
```
- Choose a `runtime` for scheduling and running the job. Currently supported runtimes are shell, docker, lava, and kubernetes. Select a runtime type from `runtimes` section. Please note that `name` property is required for `lava` and `k8s` runtimes to specify which lab/k8s context should run the test as number of LAVA labs such as BayLibre, Collabora, Qualcomm and k8s contexts have been enabled in KernelCI.
- `platforms` will include a list of device types on which the test should run. It should match entries defined in `platforms` section such as `qemu-x86`, `bcm2711-rpi-4-b`, and so on.

That's pretty much it. After following the above steps, run your pipeline instance and you are good to go with your newly added test!
