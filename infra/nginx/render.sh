#!/bin/sh
# Renders this directory into /etc/nginx/conf.d and /etc/nginx/generated for
# the configured webhook. entrypoint.sh runs it when nginx starts;
# infra/deploy/deploy.sh runs it in the running nginx before it reloads, since
# nothing nginx loads is read from this directory: a reload reads only what
# was rendered. Not named *.conf, so nginx never loads it.
set -eu

source_dir=/etc/nginx/bot
generated=/etc/nginx/generated

# The host Telegram calls: WEBHOOK_BASE_URL without the scheme, port and path.
host="${WEBHOOK_BASE_URL#https://}"
host="${host%%/*}"
host="${host%%:*}"
if [ -z "$host" ] || [ "$host" = "$WEBHOOK_BASE_URL" ]; then
  echo "nginx: WEBHOOK_BASE_URL must be an https:// URL" >&2
  exit 1
fi
export WEBHOOK_HOST="$host"

mkdir -p "$generated"

# Copied, not included from the mount: a reload would read a changed file
# before the deploy tested it, and a refused render could not put it back.
cp "$source_dir/security_headers.inc" "$generated/security_headers.inc"

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
