KernelCI fstests PoC
====================


## Overview

This page contains information about the Proof-of-Concept (PoC) project to run
fstests automatically using KernelCI's new API.  It is based on `kvm-xfstests`
which starts a local VM with `kvm` natively on the host where it is running.
This solution does not involve any Docker container as all the environment is
included in the VM image.

The first part of this README.md file explains how to set up prerequisites in order to have a working VM. Next you will find a session describing how to use this VM and run fstests on your own, by hand, and the last will guide you to run the tests using `fstests runner` automation script to config and build the kernel, then run the tests using a pre-set VM, parse the results and send them to the KernelCI API to be shared in the e-mail report.

## Prerequisites

A number of dependencies need to be installed in oder to run `kvm-xfstests` and
also to build a Linux kernel natively.

For running `kvm-xfstests`:

    sudo apt install -y qemu-kvm

For building `xfstests`: see the documentation about how to [build without a
chroot](https://github.com/tytso/xfstests-bld/blob/master/Documentation/building-xfstests.md#without-a-build-chroot).  Typically:

    sudo apt install -y \
        autoconf autoconf2.64 automake autopoint bison build-essential \
        ca-certificates debootstrap e2fslibs-dev ed fakechroot gettext git \
        golang-1.13-go libblkid-dev libdbus-1-3 libgdbm-dev libicu-dev \
        libkeyutils-dev libssl-dev libsystemd-dev libtool-bin liburcu-dev \
        lsb-release meson pkg-config python3-setuptools rsync symlinks \
        qemu-utils uuid-dev zlib1g-dev

For building the Linux kernel:

    sudo apt install -y bc bison cpio flex gcc kmod libssl-dev libelf-dev

As we are not using Docker environment, it will be necessary to install KernelCI library in the client side
for interacting with `KernelCI API` and having all databases features and Kernel CI core resources:

    pip3 install -r fstests/requirements.txt

> **Note** As a follow-up improvement, the kernel builds could be done in
> Docker containers like any regular build produced by KernelCI.  Building
> natively is to simplify the process for the PoC.

For more details, see the [quick start guide for
`kvm-xfstests`](https://github.com/tytso/xfstests-bld/blob/master/Documentation/kvm-quickstart.md)


## Setting up kvm-xfstests

The next step is to have `kvm-xfstests` installed and available in the
execution `$PATH`.

Check out the code:

    git clone https://github.com/tytso/xfstests-bld.git
    cd xfstests-bld

Download a pre-built KVM image:

    wget -O test-appliance/root_fs.img \
        https://www.kernel.org/pub/linux/kernel/people/tytso/kvm-xfstests/root_fs.img.i386

Generate the `kvm-xfstests` launcher script:

    make kvm-xfstests

Install it in your bin directory and add it to the `$PATH` if necessary:

    cp kvm-xfstests ~/bin
    export PATH=$HOME/bin:$PATH

Check it's in your path now:

    $ kvm-xfstests --help
    Usage: kvm-xfstests [<OPTIONS>] smoke|full
    Usage: kvm-xfstests [<OPTIONS>] <test> ...
    Usage: kvm-xfstests [<OPTIONS>] -g <group> ...
    Usage: kvm-xfstests [<OPTIONS>] shell|maint
    Usage: kvm-xfstests [<OPTIONS>] syz <repro>
    [...]


## Building a kernel for fstests

A particular set of kernel config options need to be enabled for fstests.  This
is automated by the `install-kconfig` command.  KernelCI automated jobs will be
using a Linux kernel source tarball, but to do check things are set up
correctly by hand we'll use a regular git checkout here.

Get the Linux kernel source tree (lightweight checkout), outside of the
`xfstests-bld` directory:

    mkdir linux
    cd linux
    git init
    git remote add origin \
        https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git
    git fetch origin --depth=1 v5.19
    git checkout FETCH_HEAD -b linux-5.19

Generate the kernel config for fstests and build the kernel image (no modules):

    kvm-xfstests install-kconfig
    make -j$(nproc)


## Running the tests

Then it's all ready to run some tests.  For example, the `smoke` tests which
should take under 30min to run:

    kvm-xfstests smoke

Or to run just a single set of tests, which should take about 1min:

    kvm-xfstests generic/001

To get the XML results file , run a shell in the VM and copy the file to the
host:

    $ kvm-xfstests shell
    # cp /results/ext4/results-4k/results.xml /vtmp/
    # shutdown -h now
    $ ls /tmp/kvm-xfstests-kernelci/results.xml

It should start with something like this after running `generic/001`:

```xml
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="xfstests" failures="0" skipped="0" tests="1" time="4" hostname="kvm-xfstests" timestamp="2022-08-08T04:09:42">
```

or after running the full `smoke` tests:

```xml
<?xml version="1.0" encoding="utf-8"?>
<testsuite name="xfstests" failures="2" skipped="17" tests="406" time="1227" hostname="kvm-xfstests" timestamp="2022-07-29T18:05:15">
```

## KernelCI automation

Now that all these steps show that the host can run `kvm-fstests`, a service
can be run to receive events from the KernelCI API whenever a new kernel source
tarball is available to test.  This will then generate a job to automatically
download the tarball, extract it, build a kernel with the config required for
fstests, run the smoke tests, parse the results and send it to the API.

To set up a local instance you first need to set instances of [Kernel CI API](https://kernelci.org/docs/api/getting-started/#setting-up-an-api-instance) and [Kernel CI Pipeline](https://kernelci.org/docs/api/getting-started/#setting-up-a-pipeline-instance) in your local environment. Once you are able to interact with the API with cURL or any tool of your preference and have also set up the pipeline, now it is time to start `fstests runner.py`.

Before you start running the script you need to get base templates from [Kernel CI core](https://github.com/kernelci/kernelci-core/), especifically, you will need `python.jinja2` and `shell-python.jinja2` templates. The easiest way to do this is to install Kernel CI core following the steps below:  

```bash
git clone https://github.com/kernelci/kernelci-core.git
cd kernelci-core
pip3 install -r requirements-dev.txt
python3 setup.py install
sudo cp -R config /etc/kernelci
```

After that you will need to set the configuration file proper, for now, we just need to set 3 variables for `fstests runner` :

Before running, it's necessary to set up the configuration file for the
Kernel CI environment. The file can be found at `<config file
path>`. For more information on how the config file works, you can refer
to [documentation](https://kernelci.org/docs/core/settings/). To run the
tests locally using Qemu+KVM (kvm-kfstests), we need to set up 3
variables for the `fstests runner` section:

- `output`: Path to a directory that could be used as during runtime as tmp.
- `xfstests_bld_path`: Path to the directory where `xfstests` was built.
- `db_config`:  Database location. You should be able to use a pre-set token to access this DB. 

To run the tests on a GCE VM instance we also need to configure these
parameters:

- `gce_project`: GCE project that will host the VMs
- `gs_bucket`: Google Storage bucket that will be used to store the tests artifacts
- `gce_zone`: GCE zone where the VMs will be created

Note that all these things (the GCE account, project and GS bucket) must
be created and configured beforehand, and the host used to launch the
tests must be authenticated in Google Cloud. See [the gce-xfstests
documentation](https://github.com/tytso/xfstests-bld/blob/master/Documentation/gce-xfstests.md)
for details on how to do that.

To run the tests locally (KVM):

```bash
python3 src/fstests/runner.py --settings src/fstests/fstests.kernelci.conf run
```

You also have the option to run fstests for a given `checkout` using its `node-id` as a parameter to run the command.

```bash
python3 src/fstests/runner.py --settings src/fstests/fstests.kernelci.conf run --node-id checkout-node-id
```

To run the tests on a GCE instance, add the `--gce` flag.
By default, the tests run will be the `smoke` collection (quick tests
and only on a 4k filesystem configuration). You can run the full
collection by specifying `--testcase full`.

With all set you are ready to just call `fstests/runner.py` and start listening for events.

`fstests/runner.py` is expecting `checkout` events with state `available`. Once you trigger a checkout using Kernel CI API, `runner.py` will take the checkout and procceed with the steps described above to build the kernel, run the tests in VM, collect the results, parse them and send them to the API.

Other command-line options that could be interesting during development
are:

- `--src-dir`: to use an existing local directory that already contains
  the kernel source instead of downloading a new tarball.
- `--skip-build`: when used together with `--src-dir`, skips the
  configuration and compilation of the kernel. Useful in case you're
  working with a local kernel code that's already configured and built.
