#!/bin/bash
# This startup script ensures that subtending services are at least responsive.
# This will also create a fund based on environment variables

END=100

# Start the main app so that the REST service will be available
./bin/cb_proxy `echo $PRODUCTS|sed "s/:/ /g"`

while [ 1 ]
do
  sleep 60 &
  wait $!
done
