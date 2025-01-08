---
title: "pipeline installation"
date: 2025-01-08
description: "Instructions to install the KernelCI pipeline"
weight: 4
---

# Installation

## Kubernetes runtime configuration

### Kbuild

The KernelCI pipeline uses the [Kbuild](https://github.com/kernelci/kernelci-core/blob/main/kernelci/kbuild.py) module to build the kernel. 
As kbuild generate pod manifests, it requires some variables to be set in the environment of *kubernetes build clusters*.

- `KCI_INSTANCE`: Type of the instance (e.g. `prod`, or anything else is detected as staging)

And several secrets:

- name:`kci-storage-tokens`, key:`production` (or `staging` for staging) - contains the storage tokens for the storage backends
- name:`kci-api-jwt-early access` (or `kci-api-jwt-staging` for staging), key:`token` - contains the JWT token for the KernelCI API

This secrets is subject to change soon, to make it more consistent with the rest of the secrets.
