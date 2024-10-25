#!/usr/bin/env python3
"""
This is simplified example how to subscribe to kernelci.org pubsub
It uses long polling to get events from the server
It is not recommended to use this in production, as it is not efficient,
and in production it is preferable to use cloudevents library
"""
import requests
import json
import sys


# This is staging server: "https://staging.kernelci.org:9000/latest"
# For production use "https://kernelci-api.westus3.cloudapp.azure.com/latest/"
BASE_URI = "https://staging.kernelci.org:9000/latest"
SUBSCRIBE_PATH = "/subscribe/node?promisc=true"
LIST_PATH = "/listen/"  # sub-id as part of path


def read_token():
    try:
        with open("token.txt", "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        print("Token file not found")
        sys.exit(1)


def subscribe_node(token):
    url = BASE_URI + SUBSCRIBE_PATH
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(url, headers=headers)
    response.raise_for_status()
    print(response.json())
    return response.json()


def pollsub(sub_id, token):
    url = BASE_URI + LIST_PATH + str(sub_id)
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


def process_event(eventdata):
    if eventdata.get("kind") == "kbuild":
        print(f"Got kbuild event: {eventdata}")
    else:
        print(f"Got unknown event: {eventdata}")


def main():
    token = read_token()
    print("Subscribing to node")
    response = subscribe_node(token)
    print(f'Subscribed with id {response["id"]}')
    # Here i skip cloudevents and parse "manually"
    # which is not recommended
    while True:
        r = pollsub(response["id"], token)
        # print(json.dumps(r, indent=2))
        if r:
            if r.get("type") == "message":
                rawjson = r["data"]
                data = json.loads(rawjson)
                if data["data"] == "BEEP":
                    # this is keepalive message
                    print("Got keepalive")
                    continue
                # here is event data
                process_event(data["data"])


if __name__ == "__main__":
    main()
