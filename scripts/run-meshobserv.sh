#!/bin/bash

docker stop meshobserv
docker rm meshobserv

# temporary code added to avoid blocklist.txt file not found
    mkdir -p /data/meshmap.net
    touch /data/meshmap.net/blocklist.txt      
#   rm /data/meshmap.net/website/nodes.json

docker run --name meshobserv \
    --restart unless-stopped \
    -d \
    -v /data:/data \
    -v /var/www/html/meshmap:/data/meshmap.net/website \
    meshobserv \
    -f /data/meshmap.net/website/nodes.json \
    -b /data/meshmap.net/blocklist.txt
    
 
