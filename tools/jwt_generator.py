#!/usr/bin/env python3
'''
Tool to add accounts for pipeline-api
'''
import jwt
import sys
import json
import argparse
import toml


def read_secret(filename):
    '''
    [jwt]
    secret=xxx
    '''
    try:
        with open(filename, 'r') as f:
            config = toml.load(f)
            return config['jwt']['secret']
    except Exception as e:
        print(f"Error reading secret: {e}")
        sys.exit(1)


def generate_jwt(payload, secret):
    return jwt.encode(payload, secret, algorithm='HS256')


def main():
    parser = argparse.ArgumentParser(description='Generate JWT token')
    parser.add_argument('--permissions', type=str,
                        help='Permissions (checkout, testretry, testfix, testpatch)',
                        default='checkout,testretry,testfix,testpatch')
    parser.add_argument('--secret', type=str, help='Secret key to sign the payload')
    parser.add_argument('--toml', type=str, help='Path to toml file containing secret key')
    parser.add_argument('--email', type=str, help='Email of maintainer', required=True)
    args = parser.parse_args()

    # remove spaces
    args.permissions = args.permissions.replace(" ", "")

    # Basic email validation
    if '@' not in args.email:
        print("Invalid email")
        sys.exit(1)

    # if comma separated permissions are provided, split them into a list
    permissions = args.permissions.split(',')
    payload = {}
    payload['permissions'] = permissions
    payload['email'] = args.email

    if args.secret is None:
        if args.toml is None:
            print("Please provide either secret or toml file")
            sys.exit(1)
        secret = read_secret(args.toml)
    else:
        secret = args.secret
    token = generate_jwt(payload, secret)
    print(f"JWT token: {token}")
    print("WARNING: Please store the token securely. This is confidential information")


if __name__ == '__main__':
    main()
