---
title: "Developer Documentation"
date: 2024-05-22
description: "KernelCI Pipeline developer manual"
weight: 4
---

## Enabling a new Kernel tree

We can monitor different kernel trees in KernelCI.
This manual describes how to enable them in [`kernelci-pipeline`](https://github.com/kernelci/kernelci-pipeline.git).


### Pipeline configuration
The pipeline [configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file has `trees` section.
In order to enable a new tree, we need to add an entry there.

```yaml
trees:
    <tree-name>:
        url: "<tree-url>"
```
For example,
```yaml
trees:
    kernelci:
        url: "https://github.com/kernelci/linux.git"
```

After adding a `trees` entry, we need to define build configurations for it.
In the same [configuration](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) file, `build_configs` section is there to specify them.
For example, we need to specify which branch to monitor of a particular tree and other build variants as well.

For instance,
```yaml
build_configs:

    kernelci_staging-mainline:
        tree: kernelci
        branch: 'staging-mainline'
        variants:
            gcc-10:
                build_environment: gcc-10
                architectures:
                    x86_64:
                        base_defconfig: 'x86_64_defconfig'
                        filters:
                            - regex: { defconfig: 'x86_64_defconfig' }
```

That's it! The tree is enabled now.
All the jobs defined under `jobs` section of [config file](https://github.com/kernelci/kernelci-pipeline/blob/main/config/pipeline.yaml) would run on the newly added tree until specified otherwise.
