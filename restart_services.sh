#!/bin/bash

FILE=".env"
inotifywait -m -e close_write $FILE | while read EVENT;
do 
  echo $EVENT      
  echo ".env file changes detected. Restarting pipeline services..."
  docker-compose down
  docker-compose up --build --no-cache
done
