#!/bin/sh
# Entrypoint of the nginx service (infra/docker-compose.yml, profile webhook),
# run before the image's own. It renders this directory for the configured
# webhook, restores the bot upstream, and starts the reload loop that puts a
# renewed certificate in use. Not named *.conf, so nginx never loads it.
set -eu

source_dir=/etc/nginx/bot
generated=/etc/nginx/generated
state=/etc/nginx/upstream

# The host Telegram calls: WEBHOOK_BASE_URL without the scheme, port and path.
host="${WEBHOOK_BASE_URL#https://}"
host="${host%%/*}"
host="${host%%:*}"
if [ -z "$host" ] || [ "$host" = "$WEBHOOK_BASE_URL" ]; then
  echo "nginx: WEBHOOK_BASE_URL must be an https:// URL" >&2
  exit 1
fi
export WEBHOOK_HOST="$host"

mkdir -p "$generated" "$state"

: >"$generated/telegram_subnets.inc"
for subnet in $(printf '%s' "$TELEGRAM_ALLOWED_SUBNETS" | tr ',' ' '); do
  printf '%s 1;\n' "$subnet" >>"$generated/telegram_subnets.inc"
done

# The default route's gateway is the compose network's bridge gateway.
# /proc/net/route holds it as little-endian hex.
gateway_hex="$(awk '$2 == "00000000" { print $3; exit }' /proc/net/route)"
if [ -z "$gateway_hex" ]; then
  echo "nginx: no default route; refusing to guess the bridge gateway" >&2
  exit 1
fi
printf '%d.%d.%d.%d 0;\n' \
  "0x$(echo "$gateway_hex" | cut -c7-8)" "0x$(echo "$gateway_hex" | cut -c5-6)" \
  "0x$(echo "$gateway_hex" | cut -c3-4)" "0x$(echo "$gateway_hex" | cut -c1-2)" \
  >"$generated/bridge_gateway.inc"

# The image's default server would answer Host "localhost" with its stock page.
rm -f /etc/nginx/conf.d/default.conf
cp "$source_dir/main.conf" /etc/nginx/conf.d/main.conf
# Only these two are substituted: nginx's own $variables stay as they are.
# shellcheck disable=SC2016
variables='${WEBHOOK_HOST} ${WEBHOOK_PATH}'
envsubst "$variables" <"$source_dir/http.conf.template" >/etc/nginx/conf.d/http.conf

certificate="/etc/letsencrypt/live/$WEBHOOK_HOST/fullchain.pem"
if [ -s "$certificate" ]; then
  envsubst "$variables" <"$source_dir/tls.conf.template" >/etc/nginx/conf.d/tls.conf
elif [ "${NGINX_REQUIRE_TLS:-0}" = "1" ]; then
  echo "nginx: no certificate at $certificate; run make cert-issue DOMAIN=$WEBHOOK_HOST" >&2
  exit 1
else
  rm -f /etc/nginx/conf.d/tls.conf
  echo "nginx: no certificate for $WEBHOOK_HOST yet; serving the ACME challenge only" >&2
fi

# infra/deploy/deploy.sh pins the upstream to single bot containers while it
# rolls the bot, and records each pin in bot_upstream.applied once nginx has
# reloaded it. The record lives in a named volume, so nginx coming back in the
# middle of a roll routes where it did. A record naming a container that no
# longer resolves is stale, and the service name is used.
upstream="$state/bot_upstream.inc"
applied="$state/bot_upstream.applied"

pin_is_live() {
  [ -s "$applied" ] || return 1
  sed -n 's/^server \([^: ]*\):.*/\1/p' "$applied" | while read -r name; do
    getent hosts "$name" >/dev/null || exit 1
  done
}

if pin_is_live; then
  cp "$applied" "$upstream"
else
  printf 'server bot:%s resolve;\n' "$WEB_SERVER_PORT" >"$upstream"
  cp "$upstream" "$applied"
fi

# certbot renews into the shared volume every 12 hours; a reload every 6 hours
# puts the renewed certificate in use with no Docker socket in either container.
if [ "$*" = "nginx -g daemon off;" ]; then
  (while sleep 6h; do nginx -s reload; done) &
fi

exec /docker-entrypoint.sh "$@"
