KernelCI Pipeline
-----------------

Modular pipeline based on the new [KernelCI
API](https://github.com/kernelci/kernelci-api).

Please refer to the [API design documentation](https://docs.kernelci.org/maestro/api/design/) and [pipeline design documentation](https://docs.kernelci.org/maestro/pipeline/pipeline-details/) for more details.

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

