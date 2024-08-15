# result_summary

Last modified: Apr 2, 2024
See [CHANGELOG](result-summary-CHANGELOG) for details.

The `result_summary` tool is a Maestro client that retrieves results
from tests and builds from a running instance and generates html or
text-based reports and summaries.

It's meant to be used to automate the extraction of frequently needed
result sets, it can be used in a single-shot mode to get result
summaries in a specified date range and as a live monitor to generate
reports on the fly as new results are published.

## How to set it up

```
$ git clone https://github.com/kernelci/kernelci-pipeline.git && cd kernelci-pipeline
$ touch .env
```

Then point it to the appropriate Maestro instance by editing the
`api_config` parameter in `config/kernelci.toml`. For instance, to point
to the current production instance:

```
$ sed -i 's/api_config = "docker-host"/api_config = "production"/' config/kernelci.toml
```

## How to use it

The tool requests data to the Maestro instance by issuing [node
queries](https://github.com/kernelci/kernelci-api/blob/main/doc/api-details.md#getting-nodes-back). The
query parameter combinations and details are defined as named presets in
[config/result-summary.yaml](../config/result-summary.yaml), where each
preset defines an action, a result type, a set of search parameters and
repositories to search from and a template to generate the report with,
and the tool translates the preset definition to the appropriate API
query.

The _action_ of a preset can be either `monitor`, to have the tool
listening for live results until stopped, or `summary` to retrieve a set
of results from a date range.

**NOTE**: `monitor` mode requires read/write access to the Maestro
instance and won't be described here.

The reports and summaries are generated in the `data/output` directory.

## Examples

Here are some examples of how to use it and some details about example
presets. Check
[config/result-summary.yaml](../config/result-summary.yaml) for more
examples.

### "watchdog-reset" test failures on mainline and linux-next

Generate a summary of the "watchdog-reset" test failures on mainline and
linux-next since yesterday:

```
docker-compose run result_summary --preset=summary-watchdog-reset-failures-mainline-next --last-updated-from=$(date --date='yesterday' --rfc-3339=date)
```

This uses the `summary-watchdog-reset-failures-mainline-next` preset:

```
summary-watchdog-reset-failures-mainline-next:
  metadata:
    action: summary
    title: "KernelCI watchdog reset test failures on mainline and next"
    template: "generic-test-results.html.jinja2"
    output_file: "watchdog-reset-failures-mainline-next.html"
  preset:
    test:
      - result: fail
        group__re: watchdog-reset
        repos:
          - tree: mainline
          - tree: next
```

The set of preset parameters specified is translated to two queries:

```
{
    'kind': 'test',
    'state': 'done',
    'result': 'fail',
    'group__re': 'watchdog-reset',
    'data.kernel_revision.tree': 'mainline',
    'updated__gt': <yesterday>,
    'updated__lt': <now>
}
```

and

```
{
    'kind': 'test',
    'state': 'done',
    'result': 'fail',
    'group__re': 'watchdog-reset',
    'data.kernel_revision.tree': 'next',
    'updated__gt': <yesterday>,
    'updated__lt': <now>
}
```

The results will be generated in
`data/output/watchdog-reset-failures-mainline-next.html`.

### Failures in tast tests

Since yesterday:

```
docker-compose run result_summary --preset=tast-failures --last-updated-from=$(date --date='yesterday' --rfc-3339=date)
```

Preset:

```
tast-failures:
  metadata:
    action: summary
    title: "General Tast test failures"
    template: "generic-test-results.html.jinja2"
    output_file: "tast-failures.html"
  preset:
    test:
      - group__re: tast
        name__ne: tast
        result: fail
        data.error_code: null
```

Rationale:
- test nodes whose name isn't `tast`
- whose group matches the regex `tast`
- that failed
- and that don't have a `data.error_code`, meaning that the runtime
  didn't fail, ie. it was the test that failed, not the runtime or
  infrastructure.

The resulting query is:

```
{
    'kind': 'test',
    'state': 'done',
    'group__re': 'tast',
    'name__ne': 'tast',
    'result': 'fail',
    'data.error_code': 'null',
    'updated__gt': <yesterday>,
    'updated__lt': <now>
}
```

More query parameters can be added to the presets, or appended in the
command line. For instance, we can narrow this search to get only the
results for x86_64 targets:

```
docker-compose run result_summary --preset=tast-failures
--last-updated-from=$(date --date='yesterday' --rfc-3339=date) --query-params "data.arch=x86_64"
```

The query will now be:

```
{
    'kind': 'test',
    'state': 'done',
    'group__re': 'tast',
    'name__ne': 'tast',
    'result': 'fail',
    'data.error_code': 'null',
    'updated__gt': <yesterday>,
    'updated__lt': <now>,
    'data.arch': 'x86_64'
}
```

### Kernel build failures

Kernel build failures in stable-rc from Aug 1 to Aug 10, 2024:

```
docker-compose run result_summary --preset=stable-rc-build-failures --last-updated-from=2024-08-01 --last-updated-to=2024-08-10
```

Preset:

```
stable-rc-build-failures:
  metadata:
    action: summary
    title: "<strong>stable-rc</strong> kernel build failures"
    template: "generic-test-results.html.jinja2"
    output_file: "stable-rc-build-failures.html"
  preset:
    kbuild:
      - result: fail
        repos:
          - tree: stable-rc
```


### Kernel build regressions

Active (still failing) kernel build regressions in stable-rc from Aug 1
to Aug 10, 2024:

```
docker-compose run result_summary --preset=active-stable-rc-build-regressions --last-updated-from=2024-08-01 --last-updated-to=2024-08-10
```

Preset:

```
active-stable-rc-build-regressions:
  metadata:
    action: summary
    title: "<strong>stable-rc</strong> kernel build regressions"
    template: "generic-regressions.html.jinja2"
    output_file: "active-stable-rc-build-regressions.html"
  preset:
    regression:
      - name__re: kbuild
        # Regressions with result = fail are "active", ie. still failing
        result: fail
        repos:
          - tree: stable-rc
```
