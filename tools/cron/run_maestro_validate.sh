#!/bin/bash

timestamp=$(date +"%Y_%m_%d-%H_%M_%S")
log_file_path="/home/kernelci/logs"
log_file_name="cron-$timestamp.log"
cd /home/kernelci
kci-dev --settings kci-dev.toml maestro validate builds --all-checkouts >> "$log_file_path/$log_file_name"
kci-dev --settings kci-dev.toml maestro validate boots --all-checkouts >> "$log_file_path/$log_file_name"
python upload_log.py $log_file_name
python email_sender.py $log_file_name
