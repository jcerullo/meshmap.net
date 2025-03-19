#!/bin/bash

docker stop meshobserv
docker rm meshobserv

docker run --name meshobserv \
    --restart unless-stopped \
    -d \
    -v /data:/data \
    -v /var/www/html/meshmap:/data/meshmap.net/website \
    meshobserv \
    -b /data/meshmap.net/blocklist.txt \
    -f /data/meshmap.net/website/nodes.json
    
