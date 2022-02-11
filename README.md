KernelCI Pipeline
-----------------

Modular pipeline based on the new [KernelCI
API](https://github.com/kernelci/kernelci-api).

To use it, first start the API.  Then start the services in this repository on
the same host:

* add a token to the environment file:
```
echo "API_TOKEN=<your token>" > .env
```

* start the services with docker-compose:
```
docker-compose up --build
```

When the services are running, the logs should look like this:

```
Starting notifier ... done
Starting runner   ... done
Starting trigger  ... done
Attaching to notifier, runner, trigger
notifier    | Listening for events...
notifier    | Press Ctrl-C to stop.
runner      | Listening for new checkout events
runner      | Press Ctrl-C to stop.
trigger     | Updating repo: /home/kernelci/data/linux
trigger     | From https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux
trigger     |  * branch                      HEAD       -> FETCH_HEAD
trigger     | From git://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux
trigger     |  * branch                      HEAD       -> FETCH_HEAD
trigger     | HEAD is now at 2a987e65025e Merge tag 'perf-tools-fixes-for-v5.16-2021-12-07' of git://git.kernel.org/pub/scm/linux/kernel/git/acme/linux
trigger     | Gathering revision meta-data
trigger     | Sending revision node to API: 2a987e65025e2b79c6d453b78cb5985ac6e5eb26
trigger     | Node id: 61b1395152e82391723054d4
runner      | Creating node
notifier    | Time                        Commit        Status    Name
notifier    | 2021-12-08 23:01:37.947562  2a987e65025e  Pass      checkout
runner      | Running test
notifier    | 2021-12-08 23:01:37.972537  2a987e65025e  Pending   check-describe
trigger exited with code 0
runner      | Getting Makefile
runner      | https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/plain/Makefile/?id=2a987e65025e2b79c6d453b78cb5985ac6e5eb26
runner      | Checking git describe
runner      | Result: PASS
notifier    | 2021-12-08 23:01:38.717247  2a987e65025e  Pass      check-describe
^CGracefully stopping... (press Ctrl+C again to force)
Stopping runner   ... done
Stopping notifier ... done
```

The `trigger` service was run only once as it's not currently configured to run
periodically.


### Setup KernelCI Pipeline on WSL

To setup `kernelci-pipeline` on WSL (Windows Subsystem for Linux), we need to enable case sensitivity for the file system.
The reason being is, Windows has case-insensitive file system by default. That prevents the creation of Linux tarball files (while running `tarball` service) with the same names but different cases i.e. one with lower case and the other with upper case. 
e.g. include/uapi/linux/netfilter/xt_CONNMARK.h and include/uapi/linux/netfilter/xt_connmark.h

To enable case sensitivity recursively inside the cloned directory, fire below command from Windows Powershell after navigating to the `kernelci-pipeline` directory on your WSL mounted drive.

```
PS C:\Users\HP> cd D:\kernelci-pipeline 
PS D:\kernelci-pipeline> (Get-ChildItem -Recurse -Directory).FullName | ForEach-Object {fsutil.exe file setCaseSensitiveInfo $_ enable}  
```
