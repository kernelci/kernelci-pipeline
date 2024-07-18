---
title: "Developer Documentation"
date: 2024-06-18
description: "KernelCI Pipeline developer manual"
weight: 4
---

## Enabling new Kernel trees, builds, and tests

We can monitor different kernel trees in KernelCI. The builds and test jobs are triggered whenever the specified branches are updated.
This manual describes how to enable trees in [`kernelci-pipeline`](https://github.com/kernelci/kernelci-pipeline.git).


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

The `<tree-name>` will be used in the other sections to refer to the newly added tree.
After adding a `trees` entry, we need to define build and test configurations for it. In the same [configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file, `jobs` section is there to specify them. `ChromeOS` specific job definitions are located in [config/jobs-chromeos.yaml](https://github.com/kernelci/kernelci-pipeline/blob/main/config/jobs-chromeos.yaml) file. Depending upon the type of the job such as build or test job, different parameters are specified:

For instance,
```yaml
jobs:

  <kbuild-job-name>:
    template: <job-template>
    kind: kbuild
    image: <docker-image-name>
    params:
      arch: <architecture>
      compiler: <compiler-name>
      cross_compile: <compiler-prefix>
      dtbs_check: <dtb-check-enabled>
      defconfig: <defconfig-name>
      fragments:
       - <fragment-name>
    rules:
      min_version:
        version: <kernel-version>
        patchlevel: <kernel-patch_level>
      tree:
        - <tree-name>
        - !<tree-name2>

  <test-job-name>:
    template: <template-name>
    kind: <kind of job, either 'job' or 'test'>
    params:
      nfsroot: <rootfs-url>
      collections: <name-of-suite>
      job_timeout: <timeout>
    kcidb_test_suite: <kcidb-mapping>
    rules:
      min_version:
        version: <kernel-version>
        patchlevel: <kernel-patch-level>
      tree:
        - <tree-name>
        - !<tree-name2>
```
Here is the description of each field:
- **`template`**: A `jinja2` template should be added to the [`config/runtime`](https://github.com/kernelci/kernelci-pipeline/tree/main/config/runtime) directory. This template will be used to generate the test definition.
- **`kind`**: The `kind` field specifies the type of job. It should be `kbuild` for build jobs, `job` for a test suite, and `test` for a single test case.
- **`image`**: The `image` field specifies the Docker image used for building and running the test. This field is optional. For example, LAVA test jobs use an image defined in the test definition template instead.
- **`params`**: The `params` field includes parameters for building the kernel (for `kbuild` jobs) or running the test. These parameters can include architecture, compiler, defconfig options, job timeout, etc.
- **`rules`**: The `rules` field defines job rules. If a test should be scheduled for a specific kernel tree, branch, or version, these rules can be specified here. The rules prefixed with `!` exclude the specified condition from job scheduling. For example, in the given scenario, the scheduler does not schedule a job if an event is received for the kernel tree `tree-name2`.
- **`kcidb_test_suite`**: The `kcidb_test_suite` field maps the KernelCI test suite name with the KCIDB test. This field is not required for build jobs (`kind: kbuild`). When adding new tests, ensure their definition is present in the `tests.yaml` file in [KCIDB](https://github.com/kernelci/kcidb/blob/main/tests.yaml).

Common patterns are often defined using YAML anchors and aliases. This approach allows for concise job definitions by reusing existing configurations. For example, a kbuild job can be defined as follows:
```yaml
  kbuild-gcc-12-arm64-preempt_rt_chromebook:
    <<: *kbuild-gcc-12-arm64-job
    params:
      <<: *kbuild-gcc-12-arm64-params
      fragments:
       - 'preempt_rt'
       - 'arm64-chromebook'
      defconfig: defconfig
    rules:
      tree:
      - 'stable-rt'
```
The test job example is:
```yaml
  kselftest-exec:
    template: kselftest.jinja2
    kind: job
    params:
      nfsroot: 'http://storage.kernelci.org/images/rootfs/debian/bookworm-kselftest/20240313.0/{debarch}'
      collections: exec
      job_timeout: 10
    kcidb_test_suite: kselftest.exec
```
Please have a look at [config/pipeline.yaml](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) and [config/jobs-chromeos.yaml](https://github.com/kernelci/kernelci-pipeline/blob/main/config/jobs-chromeos.yaml) files to check currently added job definitions for reference.

We need to specify which branch to monitor of a particular tree for trigering jobs in `build_configs`.

```yaml
build_configs:
  <name-of-variant0>:
    tree: <tree-name>
    branch: <branch-name0>

  <name-of-variant1>:
    tree: <tree-name>
    branch: <branch-name1>
```

That's it! The tree is enabled now. All the jobs defined under `jobs` section of [config file](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) would run on the specified branched for this tree.

### Schedule the job

We also need a `scheduler` entry for the newly added job to specify pre-conditions for scheduling, and defining runtime and platforms for job submissions.

For example,
```yaml
scheduler:

  - job: <kbuild-job-name>
    event: <API-pubsub-event>
    runtime:
      name: <runtime-name>

  - job: <test-job-name>
    event: <API-pubsub-event>
    runtime:
      type: <runtime-type>
      name: <runtime-name>
    platforms:
      - <device-type>
```

Here is the description of each field:
- **`job`**: Specifies the job name, which must match the name used in the `jobs` section.
- **`event`**: Specifies the API PubSub event triggering the test scheduling. For example, to trigger the `kbuild` job when new source code is published, the event is specified as:
```yaml
  event:
    channel: node
    name: checkout
    state: available
```
For a test that requires a successful completion of a build job such as `kbuild-gcc-10-arm64`, specify the event as follows:
```yaml
  event:
    channel: node
    name: kbuild-gcc-10-arm64
    result: pass
```
Here, `node` refers to the name of API PubSub channel where node events are published.
- **`runtime`**: Select a runtime for scheduling and running the job. Supported runtimes include `shell`, `docker`, `lava`, and `kubernetes`. Specify the runtime type from the `runtimes` section. Note that the `name` property is required for `lava` and `kubernetes` runtimes to specify which lab or Kubernetes context should execute the test. Several LAVA labs (such as BayLibre, Collabora, Qualcomm) and Kubernetes contexts have been enabled in KernelCI.
- **`platforms`**: Includes a list of device types on which the test should run. These should match entries defined in the `platforms` section, such as `qemu-x86`, `bcm2711-rpi-4-b`, and others.

After following these steps, run your pipeline instance to activate your newly added test configuration.
