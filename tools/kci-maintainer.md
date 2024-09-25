# kci-maintainer short guide

Note: This tool will be replaced soon by [kci-dev](https://github.com/kernelci/kci-dev)
This manual and kci-maintainer will be deprecated after that.

## Access credentials

You need to obtain your access credentials from the kernelci.org team. You can have JWT token for staging instance (if you are developing new features for kernelci project) or production instance (if you are a maintainer of some kernel tree).
After obtaining the token you need to create a file `.kci_token` in same directory where is kci-maintainer script and put the token in this file.

## WARNING!

Do not submit too many custom checkouts, especially without jobfilter, at once. Try to wait until previous one completed. Computing resources are limited and we need to share them with other users.

## Install requirements

Please install requirement packages for `kci_maintainer` tool from `tools/requirements.txt` using the below command:
```
pip install -r requirements.txt
```

## Submitting custom checkout to staging 

If you want to test some specific tree on staging you can use the `kci-maintainer` tool to submit a custom checkout for this tree.
Example usage:

```
./kci-maintainer --checkout -u https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git -b master --nojobfilter --latest-commit
```
In this example we are submitting a custom checkout for the mainline tree. We are using the `--nojobfilter` option to disable the job filter and `--latest-commit` to use the latest commit from the branch.
Note, that nojobfilter will cause higher load and will run longer, so if you need to run particular build and test, better to set it in job filter.
After submitting you will receive json data like this:

```
{"message":"OK","node":{"id":"66eaad7c2ef699e4c1831ab2","kind":"checkout","name":"checkout","path":["checkout"],"group":null,"parent":null,"state":"running","result":null,"artifacts":null,"data":{"kernel_revision":{"tree":"mainline","branch":"master","commit":"39b3f4e0db5d85aa82678d9e7bc59f5e56667e2e","url":"https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git"}},"debug":null,"jobfilter":null,"created":"2024-09-18T10:37:48.548890","updated":"2024-09-18T10:37:48.548892","timeout":"2024-09-18T15:37:48.533181","holdoff":null,"owner":"staging.kernelci.org","submitter":"user:denys@denys.com","treeid":"7196cb21c5c847de1eb571a70f4b456cce834776778d0f869fc66b2e28ed69b6","user_groups":[]}}
```

You can use the `node.id` to check the status of the custom checkout in the staging instance web interface: https://staging.kernelci.org:9000/viewer?node_id=66eaad7c2ef699e4c1831ab2

## Changing instance

If you want to change the instance where you are submitting the custom checkout you can use the `--api` option. Example usage:

```
./kci-maintainer --api production --checkout -u https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git -b master --nojobfilter --latest-commit
```


