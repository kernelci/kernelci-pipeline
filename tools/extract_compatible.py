#!/usr/bin/env python3
"""
SPDX-License-Identifier: LGPL-2.1-or-later
Copyright (C) 2024 Collabora Limited
Author: Denys Fedoryshchenko <denys.f@collabora.com>

Extract "compatible" field from device tree
Uses kernelci-pipeline configs for platforms, find entries
with missing compatible field and extract it from
latest kernel sources (git shallow copy)
"""

import os
import sys
import subprocess
import yaml
import re


def get_kernel_sources():
    """
    Get kernel sources from kernelci-pipeline
    """
    repo_url = "https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/"
    if not os.path.exists("linux"):
        subprocess.run(["git", "clone", "--depth", "1", repo_url], check=True)


def find_file(filename):
    """
    Find file in kernel sources
    """
    for root, dirs, files in os.walk("linux"):
        if filename in files:
            return os.path.join(root, filename)
    return None


def get_compatible(dtb, platform, ex_compat):
    """
    Get compatible field from device tree
    """
    get_kernel_sources()
    basename = os.path.basename(dtb)
    dtsbase = basename.replace(".dtb", ".dts")
    os.chdir("linux")
    dtsfile = find_file(dtsbase)
    if not dtsfile:
        print(f"Could not find DTS file! {dtsbase}")
        os.chdir("..")
        return
    found = False
    with open(dtsfile) as f:
        for line in f:
            if "compatible" in line:
                array_of_strings = re.findall(r'"([^"]*)"', line)
                found = True
                if not ex_compat:
                    print(f"{platform}:")
                    print(f"  compatible: {array_of_strings}")
                else:
                    # if this arrays are not equal, print warning
                    compare_arrays = set(array_of_strings) - set(ex_compat)
                    if compare_arrays:
                        print(f"  #Warning: {compare_arrays} not in {ex_compat}")

                break
    if not found:
        print(f"#Could not find compatible field in {dtsfile}")
    os.chdir("..")


def get_platforms(cfgfile):
    """
    Get platforms from kernelci-pipeline
    """
    with open(cfgfile) as f:
        try:
            yml = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            print(exc)
            return

    platforms = yml.get("platforms")
    if not platforms:
        return
    for platform in platforms:
        if platforms[platform] is None:
            continue
        if "dtb" in platforms[platform]:
            ex_compat = platforms[platform].get("compatible")
            if isinstance(platforms[platform]["dtb"], list):
                for dtb in platforms[platform]["dtb"]:
                    get_compatible(dtb, platform, ex_compat)
            else:
                get_compatible(platforms[platform]["dtb"], platform, ex_compat)


def main():
    if len(sys.argv) == 2:
        if os.path.isfile(sys.argv[1]):
            get_platforms(sys.argv[1])
        else:
            print(f"File {sys.argv[1]} does not exist!")
            print("Usage: extract_compatible.py [config.yaml]")
            print("If no arguments provided, will process all files in ../config")
    else:
        for root, dirs, files in os.walk("../config"):
            for file in files:
                if file.endswith(".yaml"):
                    print(f"Processing {os.path.join(root, file)}")
                    get_platforms(os.path.join(root, file))


if __name__ == "__main__":
    main()
