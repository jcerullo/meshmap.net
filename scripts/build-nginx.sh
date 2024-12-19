#!/bin/bash

docker build \
    --no-cache \
    --pull \
    -f "$(dirname "$0")/../Dockerfile.nginx" \
    -t nginx \
    "$(dirname "$0")/.."
