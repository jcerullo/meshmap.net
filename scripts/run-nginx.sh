#!/bin/bash

docker stop nginx
docker rm nginx

docker run --name nginx \
    --restart unless-stopped \
    -v /data:/data \
    -d nginx \
    -f /data/meshmap.net/website/index.html
