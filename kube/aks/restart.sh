#!/bin/bash
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C)  2023 Collabora Limited
# Author: Guillaume Tucker <guillaume.tucker@collabora.com>

set -e

first="\
tarball \
timeout \
monitor \
scheduler-k8s \
scheduler-lava \
"

second="\
trigger \
"

stop_pods() {
    local pods=$(\
      kubectl get pods -o name \
      | while read line; do
          echo $line | cut -d\/ -f2
      done \
    )

    for pod in $pods; do
        echo "* Stopping: $pod"
        kubectl delete pod $pod --wait=false
    done

    for pod in $pods; do
        echo "* Waiting to stop: $pod"
        kubectl wait --for=delete pod $pod
    done

    return 0
}

start() {
    local items=$1

    for item in $items; do
        echo "* Applying $item"
        kubectl apply -f "$item".yaml --wait=false
    done

    for item in $items; do
        echo "* Waiting to start: $item"
        kubectl wait --for=condition=Ready --timeout=1200s pod $item
    done

    return 0
}

stop_pods
start "$first"
start "$second"
kubectl get pods

exit 0
