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
`kubernetes_log`, `kubernetes_status`, and `kubernetes_events` artifacts and
finishes the node as `done` / `incomplete`. These describe an execution or result
delivery failure; the scheduler does not infer a kernel build failure or pass
from a pod exit code.
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
artifact records that absence. Only diagnostic status and event fields are
uploaded, not pod specs.

Each poll also collects core/v1 Events in the job namespace (requiring `list`
permission on `events`) and checkpoints observed pod status before checking the
Job outcome. This includes Pending containers with image-pull errors and failed
attempts that Kubernetes may retry; these observations never finish a node on
their own. Job and Pod events are matched by UID. A Job's `SuccessfulCreate`
event can also identify a pod deleted between polls. Unrelated pods with similar
names are not included. Events for an observed worker node within the build's
time window are included as `node_context`, not as proof of the failure's cause.

The readable log includes observed container states and an event timeline;
`kubernetes-status.json` includes previously observed pod snapshots, and
`kubernetes-events.json` contains event messages, reasons, timestamps, counts,
and reporting components. Repeated events update their occurrence counts.
An empty event response does not erase earlier evidence. Event-query failures
are reported in diagnostics without blocking reconciliation.

Event collection is shared and indexed once per cluster poll. It stops after
10 pages of 200 events, 4 MiB of normalized event data, or a 30-second request
start budget. Partial results are kept with an explicit truncation diagnostic;
these limits never affect the Job/Pod queries used to decide reconciliation.
The byte budget applies to retained diagnostic fields, not raw HTTP traffic;
one response page is decoded before that check. Socket timeouts and the existing
watchdog still cover requests that outlast the request start budget. A busy
namespace can therefore lose event coverage, but cannot trigger an unlimited
event-page scan. Matching uses object indexes instead of rescanning all events
for every active build.

History is checkpointed atomically under `logs/k8s-evidence`, covered by the
Compose scheduler's existing logs mount. Other deployments must persist this
directory to retain history across container replacement. Checkpoints contain
only diagnostic fields and are removed after the pipeline node is no longer
eligible for monitoring. History is bounded to 100 pod identities, 100 status
snapshots, and 200 events per build; truncation is reported. Checkpoint I/O
failures are logged and leave the in-memory history usable. On restart, saved
evidence is restored but failure confirmation still starts from scratch.

Checkpoints are further limited to 1 MiB each, with older observations discarded
when necessary. The directory has a 64 MiB file-content budget (including the
temporary replacement), at most 1,024 files, and a 16 MiB free-disk reserve.
Writes that exceed those limits are skipped with a warning; reconciliation can
still upload the in-memory evidence. Unchanged checkpoints are not rewritten.
Temporary files left by a killed writer are removed after one hour; completed
build checkpoints are removed on the next successful node inventory. The
in-memory cache retains at most 128 builds and reloads evicted entries from
disk when available. Each monitor must have its own checkpoint directory;
the directory budgets assume one writer, as in the Compose deployment.

Polling cannot guarantee capture before pod deletion or event expiry. Container
logs are still fetched only during final reconciliation and require an available
pod and kubelet; saved status/events do not recover lost compiler output. No
preemption, OOM, or image-pull cause is inferred from `BackoffLimitExceeded` alone.

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

