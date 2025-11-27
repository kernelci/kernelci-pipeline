---
title: "pipeline config reference"
date: 2025-01-08
description: "Reference for the KernelCI pipeline config file"
weight: 4
---

# Configuration reference

This document is a reference for options available in the KernelCI pipeline
config file.

## Config file validation

The config file is validated using the `tests/validate_yaml.py` script,
including github actions workflow. It is recommended to run this script
before committing the config file to the repository.
The script will check the config file for syntax errors, mandatory
parameters, references to non-existing keys, and other common errors.
It might not catch all errors, so please double-check the config file
before using it.

## Jobs configuration

Each job is defined in the `jobs` section of the config file. Each job
have mandatory and optional parameters.
The scheduler currently uses four kinds of jobs:

- `kbuild`: build kernels
- `job`: represent a test suite that can spawn child nodes
- `process`: run post-processing or reporting steps (for example coverage reports)
- `test`: leaf-level tests that produce a single result
The `jobs` section is a dictionary of jobs. Each job is defined as a
dictionary with a name and a set of parameters. The name of the job is
the key of the dictionary, and will be used in `scheduler` section to
define the job that will be run.

### Mandatory parameters

- **template**: string (required for `job`, `process`, and `test` jobs)
- **kind**: string (`kbuild`, `job`, `process`, `test`)
- **kcidb_test_suite**: string (mandatory for `job`, `process`, and `test` jobs; optional for `kbuild`)
- **params**: dictionary (required by templates that consume runtime parameters such as `kbuild.jinja2` or `generic.jinja2`)

The YAML validation helper (`tests/validate_yaml.py`) enforces these constraints when it runs in CI or locally.
The `template` parameter is the name of the template file that will be used
to generate the job. The template file must be located in the `config/runtime`
directory of the config file.

### params
The `params` parameter is a dictionary of parameters that will be passed
to the template file. The parameters are used to customize the job
configuration. The parameters are defined in the template file and can
be used to generate the job configuration.
The parameters are defined as key-value pairs. The value can be a string,
integer, boolean or a list of strings. The parameters can be used to
customize the job configuration. 

For kbuild jobs, the `params` possibly include:
- **arch**: string (mandatory)
- **compiler**: string (mandatory)
- **defconfig**: string or list of strings (optional)
- **fragments**: list of strings (optional)
- **cross_compile**: string (optional)

`arch` is the architecture of the kernel that will be built. Possible values
are `arm`, `arm64`, `i386`, `x86_64`, `mips`, `riscv`, `um`, might be
extended in the future.

`compiler` is the compiler that will be used to build the kernel. Possible
values are `gcc-12`, `clang-21`, might be extended in the future.

`defconfig` is the defconfig that will be used to build the kernel.

`fragments` is the list of fragments that will be used to build the kernel, at current moment they
are explained in `kernelci/kernelci-core` repository, in `config/core/build-configs.yaml`, section
`fragments`. The fragments are used to customize the kernel configuration.
IMPORTANT: also you can set parameter `CONFIG_EXTRA_FIRMWARE` in the `fragments` list, for example:
```
      - 'CONFIG_EXTRA_FIRMWARE="
      amdgpu/dcn_3_1_6_dmcub.bin
      amdgpu/gc_10_3_7_ce.bin
      amdgpu/gc_10_3_7_me.bin
      amdgpu/gc_10_3_7_mec2.bin"
```
During the kernel build, kbuild process will download linux-firmware repository and add relevant
firmware files to the kernel build.

`cross_compile` is the cross compiler prefix that will be used to build the kernel, for example
`aarch64-linux-gnu-`.

`cross_compile_compat` can be set when a compatibility toolchain is needed (for example for `um` builds).

### Optional parameters

- **rules**: dictionary
- **frequency**: string

`kbuild` only parameters:
- **kselftest**: string (`disable` or `enable` kselftest build)
- **dtbs_check**: boolean flag to enable DT validation builds

### Parameter frequency (optional)

- **Value**: [Nd][Nh][Nm]
- **Default**: none (no limit)

The frequency parameter is used to limit frequency of job execution. It is
specified as a string with a number of days, hours and minutes. For example,
`1d2h30m` means that the job can only be executed
once every day, 2 hours and 30 minutes.
Job frequency is calculated to particular tree/branch, so if you have a job
that runs on multiple branches, it will be executed independently for each
branch.

Example:
```yaml
jobs:

  baseline-arm64-mediatek: &baseline-job
    template: baseline.jinja2
    kind: job
    kcidb_test_suite: boot
    params:
      frequency: 1h
```

### Rules (optional)

We have a set of rules that can be used to filter jobs based on different criteria.

`tree:`
- **Value**: [!]string(tree name) or string:string (tree name:branch name)
- **Default**: none (no limit)

Tree and branch rules are specified here (not in `params`). Tree entries can be formatted as `<tree>:<branch>`, meaning only a given
branch is allowed for a specific tree. When prepended with `!`, it indicates
a forbidden tree/branch combination.

`min_version:`
- **Value**: version: int, patchlevel: int
- **Default**: none (no limit)

This rule allows to filter jobs based on the kernel version. The `version` field
is the major version of the kernel, and the `patchlevel` field is the minor version.
For example, `version: 4, patchlevel: 19` will only allow jobs for kernel versions
4.19 and above.

`max_version:`
- **Value**: version: int, patchlevel: int
- **Default**: none (no limit)

This rule allows to filter jobs based on the kernel version. The `version` field
is the major version of the kernel, and the `patchlevel` field is the minor version.
For example, `version: 4, patchlevel: 19` will only allow jobs for kernel versions
4.19 and below.


```
    rules:
      tree:
      - '!android'
```
This will exclude all jobs from the 'android' tree.


```
    rules:
      min_version:
        version: 4
        patchlevel: 19
      tree:
      - 'next'
      - 'sashal-next'
```
This will only allow jobs from the 'next' and 'sashal-next' trees with kernel version 4.19 and above.

```
    rules:
      tree:
      - 'kernelci:staging-next'
```
This will only allow jobs from the 'staging-next' branch of the 'kernelci' tree.


```
  rules:
    min_version:
      version: 6
      patchlevel: 1
    max_version:
      version: 6
      patchlevel: 6
    arch:
      - 'arm64'
    tree:
      - linus:master
      - stable
    branch:
      - '!stable:master'
    defconfig:
      - '!allnoconfig'
    fragments:
      - 'kselftest'
      - '!arm64-chromebook'
```

For example, the following rules definition mean the job can run only:
* with a kernel version between 6.1 and 6.6
* on arm64 devices
* when using a checkout from the `master` branch on the `linus` tree,
  or any branch except `master` on the `stable` tree
* if the kernel has been built with any defconfig except `allnoconfig`,
  using the `kselftest` fragment but not the `arm64-chromebook` one

### kselftest (optional)
- **Value**: `enable` or `disable`
- **Default**: `enable`

The `kselftest` parameter is used to enable or disable the kselftest build.
The kselftest build is additional component that is built in addition to the
kernel. It is used in `kselftest`-related jobs.

### dtbs_check (optional)
- **Value**: boolean
- **Default**: disabled

The `dtbs_check` parameter is used to enable or disable the dtbs_check build.
The dtbs_check build is additional kind of build that will verify the
device tree blobs.

## Build Configs configuration

The `build_configs` section defines which kernel trees and branches to monitor
for new commits. Each build config specifies a tree/branch combination that the
trigger service will poll for changes.

Build configs are typically defined in separate files under `config/trees/`
directory, one file per tree (e.g., `config/trees/chromiumos.yaml`).

### Mandatory parameters

- **tree**: string - Name of the tree (must match an entry in `trees` section)
- **branch**: string - Branch name to monitor

### Optional parameters

- **architectures**: list of strings - Filter to limit which architectures to build
- **frequency**: string - Limit how often checkout nodes are created

### Parameter frequency (optional)

- **Value**: [Nd][Nh][Nm]
- **Default**: none (no limit)

The frequency parameter in `build_configs` limits how often the trigger service
creates new checkout nodes for a tree/branch combination. This is useful for
trees where all jobs have frequency limits - without this setting, empty
checkout nodes would be created that show as zeros in the dashboard.

When set, the trigger service checks if a checkout node for the same tree/branch
was created within the specified time period. If so, it skips creating a new
checkout node even if a new commit is detected.

This is different from the job-level `frequency` parameter which controls how
often individual jobs run. The build_config frequency controls checkout node
creation at the source, preventing empty checkouts entirely.

**Recommendation**: Set this to match the minimum frequency of all jobs
scheduled for this tree/branch. For example, if all jobs for chromiumos have
`frequency: 1d` or higher, set `frequency: 1d` in the build_config.

Example:
```yaml
build_configs:
  chromiumos:
    tree: chromiumos
    branch: 'master'
    frequency: 1d
    architectures:
      - x86_64
      - arm64
      - arm

  chromiumos-6.6:
    tree: chromiumos
    branch: 'chromeos-6.6'
    frequency: 1d
```

In this example, new checkout nodes for chromiumos branches will only be created
once per day, even if multiple commits are pushed. This prevents empty checkout
nodes when all jobs have daily frequency limits.

**Note**: The `--force` flag on the trigger service overrides the frequency
check, allowing checkout creation regardless of the frequency setting.
