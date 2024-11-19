# Configuration reference

This document is a reference for options available in the KernelCI pipeline
config file.


## Jobs configuration

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

Tree and branch rules can be formatted as `<tree>:<branch>`, meaning only a given
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

