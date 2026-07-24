#!/usr/bin/env python3
"""
Generate a JWT accepted by KernelCI services using their unified secret.
"""

import argparse
import sys
import time

import jwt
import toml


def read_secret(filename):
    """Read ``[jwt].unified_secret`` from a KernelCI TOML file."""
    try:
        with open(filename, "r") as f:
            config = toml.load(f)
            secret = config["jwt"]["unified_secret"]
            if not isinstance(secret, str) or not secret:
                raise ValueError("[jwt].unified_secret is empty")
            return secret
    except Exception as e:
        print(f"Error reading unified secret: {e}", file=sys.stderr)
        sys.exit(1)


def generate_jwt(payload, secret):
    return jwt.encode(payload, secret, algorithm="HS256")


def main():
    parser = argparse.ArgumentParser(description="Generate JWT token")
    parser.add_argument(
        "--permissions",
        type=str,
        help="Permissions (checkout, testretry, patchset) separated by comma",
        default="checkout,testretry,patchset",
    )
    secret_group = parser.add_mutually_exclusive_group(required=True)
    secret_group.add_argument(
        "--secret", type=str, help="Secret key to sign the payload"
    )
    secret_group.add_argument(
        "--toml",
        type=str,
        help="Path to TOML file containing [jwt].unified_secret",
    )
    parser.add_argument(
        "--email", type=str, help="Email of maintainer", required=True
    )
    parser.add_argument(
        "--subject",
        type=str,
        help="KernelCI API user ID (JWT subject)",
        required=True,
    )
    parser.add_argument(
        "--origin",
        type=str,
        help="KCIDB submission origin",
        required=True,
    )
    parser.add_argument(
        "--lifetime-seconds",
        type=int,
        default=315360000,
        help="Token lifetime in seconds (default: 315360000, ten years)",
    )
    args = parser.parse_args()

    permissions = [
        permission.strip()
        for permission in args.permissions.split(",")
        if permission.strip()
    ]
    if not permissions:
        parser.error("--permissions must contain at least one permission")
    if args.lifetime_seconds <= 0:
        parser.error("--lifetime-seconds must be greater than zero")
    if args.secret is not None and not args.secret:
        parser.error("--secret must not be empty")
    for option, value in (
        ("--email", args.email),
        ("--subject", args.subject),
        ("--origin", args.origin),
    ):
        if not value.strip():
            parser.error(f"{option} must not be empty")

    issued_at = int(time.time())
    payload = {
        "sub": args.subject.strip(),
        "email": args.email.strip(),
        "origin": args.origin.strip(),
        "permissions": permissions,
        "aud": ["fastapi-users:auth"],
        "iat": issued_at,
        "exp": issued_at + args.lifetime_seconds,
    }

    secret = args.secret if args.secret is not None else read_secret(args.toml)
    token = generate_jwt(payload, secret)
    print(f"JWT token: {token}")
    print(
        "WARNING: Please store the token securely. This is confidential information"
    )


if __name__ == "__main__":
    main()
