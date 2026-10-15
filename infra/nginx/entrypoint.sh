#!/bin/sh
# Entrypoint of the nginx service (infra/docker-compose.yml, profile webhook),
# run before the image's own. It renders this directory for the configured
# webhook (render.sh), restores the bot upstream, and starts the reload loop
# that puts a renewed certificate in use. Not named *.conf, so nginx never
# loads it.
set -eu

state=/etc/nginx/upstream

sh /etc/nginx/bot/render.sh
mkdir -p "$state"

# infra/deploy/deploy.sh pins the upstream to single bot containers while it
# rolls the bot, and records each pin in bot_upstream.applied once nginx has
# reloaded it. The record lives in a named volume, so nginx coming back in the
# middle of a roll routes where it did. A record naming a container that no
# longer resolves, or naming none, is stale, and the service name is used.
upstream="$state/bot_upstream.inc"
applied="$state/bot_upstream.applied"

pin_is_live() {
  [ -s "$applied" ] || return 1
  names="$(sed -n 's/^server \([^: ]*\):.*/\1/p' "$applied")"
  [ -n "$names" ] || return 1
  for name in $names; do
    getent hosts "$name" >/dev/null || return 1
  done
}

# `nginx -t` (deploy.sh and `make cert-issue` test a configuration in a
# container of their own) shares the volume with the running nginx, whose pin
# it must leave alone: it writes the service name only where nothing is there
# yet, for its include to resolve, and never touches the record.
if [ "$*" = "nginx -t" ]; then
  [ -s "$upstream" ] || printf 'server bot:%s resolve;\n' "$WEB_SERVER_PORT" >"$upstream"
elif pin_is_live; then
  cp "$applied" "$upstream"
else
  printf 'server bot:%s resolve;\n' "$WEB_SERVER_PORT" >"$upstream"
  cp "$upstream" "$applied"
fi

# certbot renews into the shared volume every 12 hours; a reload every 6 hours
# puts the renewed certificate in use with no Docker socket in either container.
# The loop inherits `set -e`, so a failed reload must not end it.
if [ "$*" = "nginx -g daemon off;" ]; then
  (while sleep 6h; do nginx -s reload || echo "nginx: reload failed; retrying in 6h" >&2; done) &
fi

exec /docker-entrypoint.sh "$@"
