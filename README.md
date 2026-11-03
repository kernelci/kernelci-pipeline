KernelCI Pipeline
-----------------

Modular pipeline based on the new [KernelCI
API](https://github.com/kernelci/kernelci-api).

Please refer to the [API design documentation](https://docs.kernelci.org/maestro/api/design/) and [pipeline design documentation](https://docs.kernelci.org/maestro/pipeline/pipeline-details/) for more details.

### Kubernetes job reconciliation

The Kubernetes scheduler also checks its owner's running Kubernetes nodes every
10 minutes, including jobs submitted before a scheduler restart. This runs in a
background thread in the existing scheduler and uses the same cluster and
artifact storage credentials. Cluster queries are grouped by context; event
reception and job submission continue independently.

After two successful checks confirm a failed or missing Job, a Job without pods,
or a completed Job without a pipeline result, the scheduler uploads
`kubernetes_log` and `kubernetes_status` artifacts and finishes the node as
`done` / `incomplete`. These describe an execution or result delivery failure;
the scheduler does not infer a kernel build failure or pass from a pod exit code.
The node uses the API's `Infrastructure` error code; the specific `k8s_*`
outcome is retained in its error message and Kubernetes diagnostic artifacts.
Diagnosis normally takes 10–20 minutes after failure, within the current
30-minute retention of finished Jobs. The normal six-hour timeout remains a
fallback for work that is still running or pending.
Kubernetes submission and context capture share a lock across runtimes, so
concurrent scheduler threads record the cluster used by their own Job.

Pending/running pods and Jobs still retrying a failed attempt are left alone.
Cluster query errors reset confirmation instead of marking jobs missing. A
fresh node read before and after uploading diagnostics preserves results that
arrive during reconciliation. Storage failures are retried on the next poll
without completing the node. HTTP diagnostic uploads use a five-second connect
timeout and a 30-second read timeout, with at most two attempts separated by two
seconds. This retry budget is local to the monitor; normal artifact uploads keep
their existing settings. The fatal ten-minute watchdog still covers the monitor
as a backstop for operations that remain stuck despite network timeouts.
Logs are decoded from UTF-8 bytes with replacement for invalid sequences,
preserving line breaks. They include the last 10,000 lines, capped at 4 MiB
per container attempt, including init containers and previous container logs
when available. Already deleted pods cannot supply logs; their diagnostic
artifact records that absence. Only Job/Pod status is uploaded, not pod specs.

### Setting up LAVA lab

For scheduling jobs, the pipeline needs to be able to submit jobs to a "LAVA lab" type of runtime and receive HTTP(S) callbacks with results over "lava-callback" service.
Runtime is configured in yaml file following way, for example:
```
  lava-collabora: &lava-collabora-staging
    lab_type: lava
    url: https://lava.collabora.dev/
    priority_min: 40
    priority_max: 60
    notify:
      callback:
        token: kernelci-api-token-staging
```

- url is endpoint of LAVA lab API where job will be submitted.
- notify.callback.token is token DESCRIPTION used in LAVA job definition. This part is a little bit tricky: https://docs.lavasoftware.org/lava/user-notifications.html#notification-callbacks
If you specify token name that does not exist in LAVA under user submitting job, callback will return token secret set to description. If following example it will be "kernelci-api-token-staging".
If you specify token name that matches existing token in LAVA, callback will return token value (secret) from LAVA, which is usually long alphanumeric string.
Tokens generated in LAVA in "API -> Tokens" section. Token name is "DESCRIPTION" and token value (secret) can be shown by clicking on green eye icon named "View token hash".
Callback URL is set in pipeline instance environment variable KCI_INSTANCE_CALLBACK.

The `lava-callback` service is used to receive notifications from LAVA after a job has finished.  It is configured to listen on port 8000 by default and expects in header "Authorization" token value(secret) from LAVA. Mapping of token value to lab name is done over toml file. Example:
```
[runtime.lava-collabora]
runtime_token = "REPLACE-LAVA-TOKEN-GENERATED-BY-LAB-LAVA-COLLABORA"
callback_token = "REPLACE-LAVA-TOKEN-GENERATED-BY-LAB-LAVA-COLLABORA"
```
In case we have single token, it will be same token used to submit job(by scheduler), runtime_token only, but if we use different to tokens to submit job and to receive callback, we need to specify both runtime_token and callback_token.

Summary: Token name(description) is used in yaml configuration, token value(secret) is used in toml configuration.

### Setup KernelCI Pipeline on WSL

To setup `kernelci-pipeline` on WSL (Windows Subsystem for Linux), we need to enable case sensitivity for the file system.
The reason being is, Windows has case-insensitive file system by default. That prevents the creation of Linux tarball files (while running `tarball` service) with the same names but different cases i.e. one with lower case and the other with upper case. 
e.g. include/uapi/linux/netfilter/xt_CONNMARK.h and include/uapi/linux/netfilter/xt_connmark.h

To enable case sensitivity recursively inside the cloned directory, fire the below command from Windows Powershell after navigating to the `kernelci-pipeline` directory on your WSL mounted drive.

```
PS C:\Users\HP> cd D:\kernelci-pipeline 
PS D:\kernelci-pipeline> (Get-ChildItem -Recurse -Directory).FullName | ForEach-Object {fsutil.exe file setCaseSensitiveInfo $_ enable}  
```

### KCIDB setup 

* add KCIDB_REST variable to the environment file:
```
echo "KCIDB_REST=<your KCIDB REST API endpoint with token>" >> .env
```

