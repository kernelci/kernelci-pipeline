#!/usr/bin/env python3
"""
This example will retrieve the latest events from the KernelCI API
and print them to the console.
It will filter only completed kernel builds, limit to 100 events per request,
and retrieve corresponding nodes with artifacts.
"""
import requests
import json
import sys
import time


# This is staging server: "https://staging.kernelci.org:9000/latest"
# For production use "https://kernelci-api.westus3.cloudapp.azure.com/latest/"
BASE_URI = "https://staging.kernelci.org:9000/latest"
EVENTS_PATH = "/events"

# Start from the beginning of time, but you might implement
# saving last processed timestamp to a file or database
timestamp = "1970-01-01T00:00:00.000000"

def pollevents(timestamp):
    url = BASE_URI + EVENTS_PATH + f"?kind=kbuild&state=done&limit=100&recursive=true&from={timestamp}"
    print(url)
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def main():
    global timestamp
    while True:
        try:
            events = pollevents(timestamp)
            if len(events) == 0:
                print("No new events, sleeping for 30 seconds")
                time.sleep(30)
                continue
            print(f"Got {len(events)} events")
            for event in events:
                print(json.dumps(event, indent=2))
                timestamp = event["timestamp"]
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
