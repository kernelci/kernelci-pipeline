#!/bin/bash

# Runs pipeline services in a debugging-friendly way.

# Without arguments, it builds and starts the docker containers for all
# services and it leaves the monitor running.
# When called with arguments, the first one is the service to run, the
# rest is the set of commands to run on it.
#
# If the command to run is "shell", it starts an interactive bash
# session on the specified service.
#
# It's recommended to run each service in a separate terminal

# EXAMPLES!
#
# 1: start the containers
#     ./debug_run.sh
#
# 2: run the tarball service
#     ./debug_run.sh tarball run
#
# 3: run the trigger service
#     ./debug_run.sh trigger run
#
# 4: run the scheduler service with the shell runtime
#     ./debug_run.sh scheduler loop --runtimes=shell
#
# 5: run an interactive shell on the scheduler service
#     ./debug_run.sh scheduler shell


stage=$1

if [ $# -eq 0 ]
then
    docker-compose -f docker-compose-debug.yaml up --build
    exit
fi

stage=$1
shift
cmds=($@)

if [ ${cmds[0]} = "shell" ]
then
    docker-compose -f docker-compose-debug.yaml exec $stage bash
else
    docker-compose -f docker-compose-debug.yaml exec $stage ./pipeline/$stage.py --settings=${KCI_SETTINGS:-/home/kernelci/config/kernelci.toml} $@
fi
