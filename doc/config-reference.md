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
