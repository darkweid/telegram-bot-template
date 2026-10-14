#!/bin/sh
# Hands the password to redis-server on stdin: an argument would show in `ps`
# and would break on a password with spaces or quotes. Goes through the image's
# own entrypoint, which drops root, fixes /data and keeps redis-server as PID 1
# so `docker stop` lets it save a final snapshot.
set -eu

escaped=$(printf '%s' "$REDIS_PASSWORD" | sed 's/[\\"]/\\&/g')
exec docker-entrypoint.sh redis-server - <<CONFIG
include /etc/redis/redis.conf
requirepass "$escaped"
CONFIG
