KernelCI Pipeline
-----------------

Modular pipeline based on the new [KernelCI
API](https://github.com/kernelci/kernelci-api).

Please refer to the [pipeline design documentation](https://docs.kernelci.org/api_pipeline/api/design/#pipeline-design) for more details.

To use it, first, start the API.  Then start the services in this repository on the same host.

Follow instructions to [add a token and start the services](https://docs.kernelci.org/api_pipeline/api/local-instance/#setting-up-a-pipeline-instance).

> **Note** The `trigger` service was run only once as it's not currently configured to run periodically.

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

[runtime.lava-collabora-early-access]
runtime_token = "REPLACE-LAVA-TOKEN-GENERATED-BY-LAB-LAVA-COLLABORA-EARLY-ACCESS"
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

* add project ID to the environment file:
```
echo "KCIDB_PROJECT_ID=<your project id>" >> .env
```

* add a topic name to the environment file:
```
echo "KCIDB_TOPIC_NAME=<your topic name>" >> .env
```
* add the path to your Google Application Credentials file to the environment file:
```
echo "GOOGLE_APPLICATION_CREDENTIALS=<your Google Application Credentials path>" >> .env
```

#### Generate Google Credentials key and copy to KCIDB directory

To set up and submit data to KCIDB, we need to get Google Credentials key. 

[Check here to see how to get Google Credentials Key.](https://github.com/kernelci/kcidb/blob/main/doc/administrator_guide.md)

NOTE: This key allows anyone to do anything with the specified Google Cloud project, so keep it safe.

Once you have gotten the Google credentials key file, copy and paste it to data/kcidb 

Use the below command to copy the Google credentials file to data/kcidb directory.
```
$ cp ~/kernelci-production-admin.json  data/kcidb/<your GOOGLE_APPLICATION_CREDENTIALS path>
```

kcidb docker container will now have /home/kernelci/data/kcidb/kernelci-production-admin.json file.

Now, the user will be able to send data to kcidb using the Google Credentials key.

#### Get KCIDB_PROJECT_ID

Once Google Credentials key file is generated, the file will have project_id key.

The value of the kcidb_project_id should be set to the env file

```
echo "KCIDB_PROJECT_ID=<project_id>" >> .env
```

#### Get KCIDB_TOPIC_NAME
The default topic name is kcidb_new. Feel free to use a different topic name, as long as the topic exists in your Google Cloud Account.

The value of the kcidb_topic_name should be set to the env file

```
echo "KCIDB_TOPIC_NAME=kcidb_new" >> .env
```


