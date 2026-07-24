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
- `KCI_INSTANCE_CALLBACK`: Base URL used by services (such as `scheduler-lava`) to expose callback endpoints

And several secrets:

- name:`kci-storage-tokens`, key:`production` (or `staging` for staging) - contains the storage tokens for the storage backends
- name:`kci-api-jwt-early access` (or `kci-api-jwt-staging` for staging), key:`token` - contains the JWT token for the KernelCI API

Keep the secret names aligned with the manifests (for example `kube/aks/scheduler-lava.yaml`) when rolling new credentials.

## JWT secret for kernelci-pipeline API

The lava-callback provides an API to the kci-dev tool, providing endpoints for custom checkouts, patchset testing, job retries, etc.

## TOML

In the configuration file, you need to have the following section:
```
[jwt]
secret = "ABCDEFGH..."
# Optional: shared HS256 key accepted as a fallback alongside `secret`.
# Set to the same value used by kernelci-api (UNIFIED_SECRET),
# kernelci-storage (unified_secret) and kcidb-restd-rs (UNIFIED_SECRET)
# so a single token authenticates a user across all KernelCI services.
# See UNIFIED_TOKEN.md in the kernelci-deploy repo for the full spec.
#unified_secret = "ABCDEFGH..."
```

Generate either secret with:
```
openssl rand -hex 32
```

The pipeline `lava-callback` validates incoming JWTs against `secret` first
and falls back to `unified_secret` on signature failure (see
`decode_jwt()` in `src/lava_callback.py`).

## Generating tokens for user

Use `jwt_generator.py` in the tools directory to generate a unified user token.
The subject is the user's ID in `kernelci-api`; the origin identifies the
submitter to KCIDB:

```
jwt_generator.py --toml kernelci.toml \
  --email user@email.com \
  --subject 65265305c74695807499037f \
  --origin kernelci-pipeline
```

The tool reads `[jwt].unified_secret` when `--toml` is used. Alternatively,
pass the same key directly with `--secret`. Tokens contain the claims required
by `kernelci-api`, `kernelci-pipeline`, `kernelci-storage`, and
`kcidb-restd-rs`, and expire after ten years by default. Use
`--lifetime-seconds` to select a shorter lifetime.

The default permissions include `checkout`, `patchset`, and `testretry`,
including the `patchset` endpoint provided by
[kernelci-pipeline#1563](https://github.com/kernelci/kernelci-pipeline/pull/1563).
Restrict them to the operations the user needs with `--permissions`.
