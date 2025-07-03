#!/usr/bin/env python3
"""
This example will retrieve the latest events from the KernelCI API
and print them to the console.
It will filter only completed kernel builds, limit to 100 events per request,
and retrieve corresponding nodes with artifacts.
"""
import argparse
import requests
import json
import sys
import time


# This is staging server: "https://staging.kernelci.org:9000/latest"
# For production use "https://api.kernelci.org/latest/"
BASE_URI = "https://staging.kernelci.org:9000/latest"
EVENTS_PATH = "/events"

# Start from the beginning of time, but you might implement
# saving last processed timestamp to a file or database
timestamp = "1970-01-01T00:00:00.000000"


"""
kind:

There are a few different kinds:

* checkout: a new git tree checkout to be tested. Maestro frequently cuts
    new tree checkout from tree it is subscribed to. See 'config/pipeline.yaml'
* kbuild: a new kernel build for a given config and arch
* job: the execution of a test suite
* test: the execution of a test inside a job


state:

In this example we track state=done to get an event when Maestro is ready to
provide all the information about the node. Eg for checkout it will provide
the commit hash to test and for builds the location of the kernel binaries built.
"""

def pollevents(timestamp, kind):
    url = BASE_URI + EVENTS_PATH + f"?state=done&kind={kind}&limit=100&recursive=true&from={timestamp}"
    print(url)
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


def main():
    global timestamp

    parser = argparse.ArgumentParser(description="Listen to events in Maestro.")
    parser.add_argument("--kind", default="kbuild", help="The kind of events")
    args = parser.parse_args()
    while True:
        try:
            events = pollevents(timestamp, args.kind)
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
