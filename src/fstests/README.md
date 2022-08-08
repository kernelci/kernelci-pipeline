KernelCI fstests PoC
====================


## Overview

This page contains information about the Proof-of-Concept (PoC) project to run
fstests automatically using KernelCI's new API.  It is based on `kvm-xfstests`
which starts a local VM with `kvm` natively on the host where it is running.
This solution does not involve any Docker container as all the environment is
included in the VM image.


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
tarbll is available to test.  This will then generate a job to automatically
download the tarball, extract it, build a kernel with the config required for
fstests, run the smoke tests, parse the results and send it to the API.

> **Warning** This needs to be implemented as part of this PoC!  See the
> [GitHub workboard](https://github.com/orgs/kernelci/projects/15/views/4) for
> more details about the current status.
