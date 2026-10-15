#!/usr/bin/env bash
# Deploys the stack on the box. Run from anywhere inside the checkout.
#
# Environment:
#   BOT_IMAGE - the image to run, e.g. ghcr.io/<owner>/<repo>:sha-<12>. Taken
#               from the shell, else from .env; empty means the local tag that
#               BUILD=1 builds.
#   BUILD     - 1 builds the image on the box from this checkout (the bootstrap
#               path, before any registry exists); 0 pulls BOT_IMAGE, the image
#               CI built and pushed (the CD path: a broken build fails in CI,
#               not halfway through a deploy). Defaults to 0 when BOT_IMAGE is
#               set and to 1 otherwise.
#   PRUNE     - 1 (default) removes images and build cache unused for a week at
#               the end; 0 keeps them. Skipped on anything but Linux, so a run
#               on a developer's machine never empties its Docker.
#
# Order matters: the checks first, then data services, migrations, the bot,
# then the worker and the scheduler. A failed check or migration aborts with
# the previous stack still running.
set -euo pipefail

# Every failure leaves with exit 1 and a [deploy] line: a step that fails under
# errexit gets the generic line here, a refusal with its own message (die) is
# not repeated.
reported=0
die() {
  echo "$1"
  reported=1
  exit 1
}
report_failure() {
  local status=$?
  if [ "$status" -ne 0 ]; then
    [ "$reported" = 1 ] || echo "[deploy] the deploy failed (exit ${status})"
    exit 1
  fi
}
trap report_failure EXIT

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

test -f .env || {
  die "[deploy] .env is missing on the box - copy .env.example and fill it in"
}
python3 scripts/ops/check_env.py .env

# Read through the checker, never by sourcing .env: a value is data, not shell.
env_value() {
  python3 scripts/ops/check_env.py --get "$1" .env
}

BOT_MODE="$(env_value BOT_MODE)"
BOT_MODE="${BOT_MODE:-polling}"
BOT_IMAGE="${BOT_IMAGE:-$(env_value BOT_IMAGE)}"
if [ -n "$BOT_IMAGE" ]; then
  BUILD="${BUILD:-0}"
else
  BUILD="${BUILD:-1}"
fi
PRUNE="${PRUNE:-1}"

case "$BUILD" in 0 | 1) ;; *) die "[deploy] BUILD must be 0 or 1, not '${BUILD}'" ;; esac
case "$PRUNE" in 0 | 1) ;; *) die "[deploy] PRUNE must be 0 or 1, not '${PRUNE}'" ;; esac

if [ "$BUILD" = "0" ] && [ -z "$BOT_IMAGE" ]; then
  die "[deploy] BUILD=0 requires BOT_IMAGE - the local tag exists in no registry"
fi
if [ "$BUILD" = "1" ] && [ -n "$BOT_IMAGE" ]; then
  # A build would put this checkout's code under the pinned tag, and the next
  # pull of that tag would silently replace it.
  die "[deploy] BUILD=1 builds the local image; unset BOT_IMAGE, or deploy it with BUILD=0"
fi
# infra/docker-compose.yml's default for BOT_IMAGE, and LOCAL_BOT_IMAGE in the
# Makefile (tests/unit/test_deploy_script.py).
BOT_IMAGE="${BOT_IMAGE:-tg-bot-template-bot:latest}"
# Consumed by infra/docker-compose.yml for bot-builder, migrate, bot, worker
# and scheduler; the shell's value wins over .env.
export BOT_IMAGE

COMPOSE=(docker compose --env-file .env -f infra/docker-compose.yml)

# A new bot container gets this long to turn healthy before it is removed. The
# healthcheck in infra/docker-compose.yml reports one that never answers
# unhealthy sooner (start_period plus retries x interval, 70s).
BOT_HEALTHY_TIMEOUT_SECONDS=120
# The worker and the scheduler get this long to come up. The scheduler's
# healthcheck reports one that never beats unhealthy after start_period plus
# retries x interval, 130s.
TASKS_HEALTHY_TIMEOUT_SECONDS=180
# The worker has no healthcheck, so `--wait` passes the moment it runs; it
# must still be running, never restarted, this long after.
TASKS_SETTLE_SECONDS=15
ROLLBACK_FAILED="[deploy] rollback failed; the bot, worker or scheduler may still run the new image beside the previous one - check 'docker compose ps' and the logs, then deploy the previous image again"
# How long the nginx workers retired by a reload get to finish the requests
# they still carry to the old bot.
NGINX_DRAIN_TIMEOUT_SECONDS=60

nginx_is_running() {
  [ -n "$("${COMPOSE[@]}" ps -q nginx)" ]
}

# nginx routes to whatever /etc/nginx/upstream/bot_upstream.inc names (the
# upstream in infra/nginx/main.conf). It is written inside the nginx container,
# not in the checkout: it changes on every webhook deploy. Naming single
# containers keeps nginx off a new one until it is healthy and off an old one
# before it stops, which Docker's service name cannot do - it resolves to every
# container of the service, started or stopping. A reload finishes the requests
# in flight; a restart would drop them.
#
# bot_upstream.applied records the pin only once the reload has been sent, so it
# always names what the running nginx routes to: pinned_bot_names reads it after
# an interrupted deploy, and infra/nginx/entrypoint.sh restores it when nginx
# restarts.
route_bot_to() {
  if ! nginx_is_running; then
    echo "[deploy] nginx is not running; the bot cannot be routed"
    return 1
  fi
  # shellcheck disable=SC2016  # expanded by the shell inside the nginx container
  "${COMPOSE[@]}" exec -T nginx sh -c '
    for host in "$@"; do
      printf "server %s:%s resolve;\n" "$host" "$WEB_SERVER_PORT"
    done > /etc/nginx/upstream/bot_upstream.inc.next
    mv /etc/nginx/upstream/bot_upstream.inc.next /etc/nginx/upstream/bot_upstream.inc
  ' sh "$@" || return 1
  "${COMPOSE[@]}" exec -T nginx nginx -s reload || return 1
  "${COMPOSE[@]}" exec -T nginx sh -c '
    cp /etc/nginx/upstream/bot_upstream.inc /etc/nginx/upstream/bot_upstream.applied.next
    mv /etc/nginx/upstream/bot_upstream.applied.next /etc/nginx/upstream/bot_upstream.applied
  ' || return 1
}

# The workers a reload retires keep the requests they already accepted and
# forward those to the upstream they were started with. The old bot has to stay
# up until they are gone.
wait_for_retired_nginx_workers() {
  nginx_is_running || return 0
  local deadline=$((SECONDS + NGINX_DRAIN_TIMEOUT_SECONDS))
  # `nginx -s reload` returns once the master is signalled, before it retires
  # anything.
  sleep 1
  while "${COMPOSE[@]}" exec -T nginx sh -c \
    'grep -qs "^nginx: worker process is shutting down" /proc/[0-9]*/cmdline'; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "[deploy] nginx workers retired ${NGINX_DRAIN_TIMEOUT_SECONDS}s ago still hold connections; stopping the old bot anyway"
      return 0
    fi
    sleep 1
  done
}

# Healthy by the container's own healthcheck, not the service's: the old
# container is healthy too. A restart counts as a failure - the restart policy
# would otherwise hide a crash loop behind a fresh `starting`.
wait_until_healthy() {
  local container="$1" name="$2"
  local deadline=$((SECONDS + BOT_HEALTHY_TIMEOUT_SECONDS))
  local state
  while true; do
    state="$(docker inspect -f '{{.State.Status}} {{.RestartCount}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$container")"
    case "$state" in
      "running 0 healthy") return 0 ;;
      "running 0 starting") ;;
      *)
        echo "[deploy] ${name} is ${state:-gone}"
        return 1
        ;;
    esac
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "[deploy] ${name} did not turn healthy within ${BOT_HEALTHY_TIMEOUT_SECONDS}s"
      return 1
    fi
    sleep 2
  done
}

container_names() {
  local id
  for id in "$@"; do
    docker inspect -f '{{.Name}}' "$id" | sed 's|^/||'
  done
}

# The bot containers that are running and passing their own healthcheck - the
# only ones nginx may be pinned to. A deploy interrupted mid-roll can leave a
# second container behind that is still starting, unhealthy or crash-looping.
healthy_bot_ids() {
  local id
  for id in $("${COMPOSE[@]}" ps -a -q bot); do
    if [ "$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$id")" = "running healthy" ]; then
      echo "$id"
    fi
  done
}

# The containers the running nginx is pinned to by name, one per line; nothing
# while it routes to the service name.
pinned_bot_names() {
  nginx_is_running || return 0
  "${COMPOSE[@]}" exec -T nginx sh -c 'cat /etc/nginx/upstream/bot_upstream.applied 2>/dev/null' \
    | sed -n 's/^server \([^: ]*\):.*/\1/p' | grep -vx bot || true
}

# The healthy containers that serve: the ones nginx is pinned to when an
# interrupted roll left it pinned to healthy ones - after the cutover that is
# the new container, and the old one it no longer routes to must not come back
# - otherwise every healthy one.
serving_bot_ids() {
  local healthy pinned id serving=""
  healthy="$(healthy_bot_ids)"
  pinned="$(pinned_bot_names)"
  if [ -n "$pinned" ]; then
    for id in $healthy; do
      if printf '%s\n' "$pinned" | grep -qxF "$(container_names "$id")"; then
        serving="${serving}${id}"$'\n'
      fi
    done
  fi
  if [ -n "$serving" ]; then
    printf '%s' "$serving"
  elif [ -n "$healthy" ]; then
    printf '%s\n' "$healthy"
  fi
}

# Webhook mode: rolls the bot to the image given with no update refused. Pin
# nginx to the healthy serving containers, start one more beside them, wait for
# it to turn healthy, move nginx onto it, let the retired nginx workers finish,
# then stop the old containers - SIGTERM makes a bot finish the updates in
# progress within SHUTDOWN_TIMEOUT_SECONDS, which stop_grace_period covers - and
# point nginx back at the service name. Two bots side by side are safe here:
# each update reaches exactly one of them through nginx. A new container that
# never turns healthy is removed without having served anything.
#
# Callers test it in an `if` or after `||`, which switches errexit off for
# everything inside, so every step that must not be skipped checks its own
# status.
roll_bot() {
  local image="$1"
  local all_ids old_ids leftover_ids new_id new_name id count=1

  # A stopped container of the service would be started again by the scale-up
  # below and counted as one of its replicas.
  "${COMPOSE[@]}" rm -f bot >/dev/null || return 1

  all_ids="$("${COMPOSE[@]}" ps -a -q bot)" || return 1
  old_ids="$(serving_bot_ids)" || return 1
  if [ -n "$old_ids" ]; then
    leftover_ids=""
    for id in $all_ids; do
      if ! printf '%s\n' "$old_ids" | grep -qxF "$id"; then
        leftover_ids="${leftover_ids} ${id}"
      fi
    done
    # nginx is pinned before any leftover stops: an interrupted deploy may have
    # reloaded nginx onto a container without recording it as applied.
    # On a switch from polling, the old container pins nginx until the cutover;
    # updates in between get a 404 and Telegram retries them.
    # shellcheck disable=SC2046,SC2086  # container ids and names, one word each
    route_bot_to $(container_names $old_ids) || return 1
    if [ -n "$leftover_ids" ]; then
      wait_for_retired_nginx_workers
      for id in $leftover_ids; do
        echo "[deploy] stopping $(container_names "$id"), left behind by an interrupted deploy and no longer routed to"
        docker stop "$id" >/dev/null || return 1
        docker rm "$id" >/dev/null || return 1
      done
    fi
  else
    # Nothing is verified to serve, so there is nothing to pin nginx to; the
    # containers still count as old ones and stop once the new one is healthy.
    old_ids="$all_ids"
    if [ -n "$old_ids" ]; then
      echo "[deploy] no bot container is healthy; the new one takes over once it is"
    fi
  fi
  for id in $old_ids; do
    count=$((count + 1))
  done

  # --no-recreate leaves the serving containers alone and adds one more from the
  # current configuration; --no-deps keeps bot-builder and migrate out of it.
  if ! BOT_IMAGE="$image" "${COMPOSE[@]}" up -d --no-deps --no-recreate --scale "bot=${count}" bot; then
    echo "[deploy] starting the new bot container failed"
    discard_new_bot "$old_ids"
    return 1
  fi

  new_id=""
  for id in $("${COMPOSE[@]}" ps -a -q bot); do
    printf '%s\n' "$old_ids" | grep -qxF "$id" || new_id="$id"
  done
  if [ -z "$new_id" ]; then
    echo "[deploy] no new bot container was started"
    route_bot_to bot || true
    return 1
  fi
  new_name="$(container_names "$new_id")"
  echo "[deploy] waiting for ${new_name} to turn healthy"

  if ! wait_until_healthy "$new_id" "$new_name"; then
    echo "[deploy] last log lines of ${new_name}:"
    docker logs --tail 50 "$new_id" 2>&1 || true
    discard_new_bot "$old_ids"
    return 1
  fi

  if ! route_bot_to "$new_name"; then
    echo "[deploy] nginx could not be moved onto ${new_name}"
    discard_new_bot "$old_ids"
    return 1
  fi
  if [ -n "$old_ids" ]; then
    wait_for_retired_nginx_workers
    echo "[deploy] stopping the previous bot container"
    # shellcheck disable=SC2086  # container ids, one word each
    docker stop $old_ids >/dev/null || return 1
    # shellcheck disable=SC2086
    docker rm $old_ids >/dev/null || return 1
  fi
  route_bot_to bot || return 1
}

# Removes every bot container the roll started, the ones given excepted. nginx
# goes back to those first. Best effort: it runs on a path that already failed.
discard_new_bot() {
  local keep="$1" id
  if [ -n "$keep" ]; then
    # shellcheck disable=SC2046,SC2086  # container ids and names, one word each
    route_bot_to $(container_names $keep) || true
    wait_for_retired_nginx_workers || true
  fi
  for id in $("${COMPOSE[@]}" ps -a -q bot); do
    if ! printf '%s\n' "$keep" | grep -qxF "$id"; then
      docker stop "$id" >/dev/null 2>&1 || true
      docker rm -f "$id" >/dev/null || true
    fi
  done
  route_bot_to bot || true
}

# Recreates the worker and the scheduler on the image in BOT_IMAGE. A restart
# counts as a failure, as for the bot: the restart policy would otherwise hide
# a crash loop. Callers test it in an `if` or after `||`, so every step checks
# its own status.
replace_worker_and_scheduler() {
  local service id state
  "${COMPOSE[@]}" up -d --no-deps --wait \
    --wait-timeout "$TASKS_HEALTHY_TIMEOUT_SECONDS" worker scheduler || return 1
  sleep "$TASKS_SETTLE_SECONDS"
  for service in worker scheduler; do
    for id in $("${COMPOSE[@]}" ps -a -q "$service"); do
      state="$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$id")" || return 1
      if [ "$state" != "running 0" ]; then
        echo "[deploy] ${service} is ${state:-gone} ${TASKS_SETTLE_SECONDS}s after it started"
        return 1
      fi
    done
  done
}

# Polling mode: one bot per token, ever - two pollers make Telegram answer
# both with a conflict. The container is replaced in place; updates wait at
# Telegram for the few seconds it takes.
replace_polling_bot() {
  "${COMPOSE[@]}" up -d --no-deps --force-recreate --wait \
    --wait-timeout "$BOT_HEALTHY_TIMEOUT_SECONDS" bot
}

if [ "$BOT_MODE" = "webhook" ]; then
  # The nginx configuration this checkout ships is tested in a fresh container
  # with the new mounts before anything changes, and NGINX_REQUIRE_TLS makes a
  # missing certificate fail here: a webhook bot behind nginx without TLS would
  # never receive an update. The upstream is resolved at run time, so the test
  # needs no bot container and runs on a first deploy too.
  echo "[deploy] testing the nginx configuration and the certificate"
  "${COMPOSE[@]}" run --rm --no-deps -e NGINX_REQUIRE_TLS=1 nginx nginx -t
fi

# The image the running bot runs, recorded before anything changes, so the
# whole stack can go back to it when the bot, the worker or the scheduler never
# turns healthy. The image id, not the tag: BUILD=1 rebuilds the same tag,
# which would then name the new image. Empty on the first deploy.
PREVIOUS_IMAGE=""
if [ "$BOT_MODE" = "webhook" ]; then
  RUNNING_BOT="$(serving_bot_ids)"
else
  RUNNING_BOT="$("${COMPOSE[@]}" ps -q bot)"
fi
# The first line taken in the shell, not with `head`: head exits after one
# line, and the writer's SIGPIPE would fail the pipeline under pipefail.
RUNNING_BOT="${RUNNING_BOT%%$'\n'*}"
if [ -n "$RUNNING_BOT" ]; then
  PREVIOUS_IMAGE="$(docker inspect -f '{{.Image}}' "$RUNNING_BOT")"
fi

# The database image stays box-local in both modes: it carries
# postgresql.conf, and `up` below recreates the container when it changed.
echo "[deploy] building the postgres image"
"${COMPOSE[@]}" build --pull postgres

if [ "$BUILD" = "1" ]; then
  # The image's version stamp (bot-builder's build argument).
  COMMIT_SHA="$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
  export COMMIT_SHA
  echo "[deploy] building ${BOT_IMAGE} on the box"
  "${COMPOSE[@]}" build bot-builder
else
  echo "[deploy] pulling ${BOT_IMAGE}"
  "${COMPOSE[@]}" pull bot
fi

echo "[deploy] starting data services"
"${COMPOSE[@]}" up -d --wait postgres redis

echo "[deploy] applying migrations before any new code runs"
"${COMPOSE[@]}" run --rm --no-deps migrate

if [ "$BOT_MODE" = "webhook" ]; then
  # Before the roll, which relies on the running nginx reading bot_upstream.inc.
  # Starts nginx on the first deploy; later a no-op unless the service itself
  # changed in infra/docker-compose.yml, which recreates it.
  NGINX_BEFORE="$("${COMPOSE[@]}" ps -q nginx)"
  "${COMPOSE[@]}" up -d --no-deps nginx certbot
  # A started or recreated nginx rendered this checkout's configuration on its
  # way up. One left running still serves what it rendered when it started: a
  # reload reads /etc/nginx/conf.d, never the mounted directory, so the files
  # tested above are rendered into it here, tested there and reloaded.
  if [ -n "$NGINX_BEFORE" ] && [ "$NGINX_BEFORE" = "$("${COMPOSE[@]}" ps -q nginx)" ]; then
    echo "[deploy] rendering the nginx configuration into the running nginx"
    "${COMPOSE[@]}" exec -T -e NGINX_REQUIRE_TLS=1 nginx sh /etc/nginx/bot/render.sh
    "${COMPOSE[@]}" exec -T nginx nginx -t
    "${COMPOSE[@]}" exec -T nginx nginx -s reload
  fi
  echo "[deploy] rolling the bot"
  if ! roll_bot "$BOT_IMAGE"; then
    die "[deploy] rolling the bot failed; the previous one keeps serving and the deploy failed"
  fi
else
  # Left from webhook mode: nginx would keep ports 80 and 443 open with no bot
  # behind the webhook path.
  "${COMPOSE[@]}" --profile webhook rm -sf nginx certbot
  echo "[deploy] replacing the bot"
  if ! replace_polling_bot; then
    if [ -z "$PREVIOUS_IMAGE" ]; then
      die "[deploy] the bot did not become healthy and there is no previous image to go back to"
    fi
    echo "[deploy] the bot did not become healthy; going back to ${PREVIOUS_IMAGE}"
    if BOT_IMAGE="$PREVIOUS_IMAGE" replace_polling_bot; then
      die "[deploy] rolled back; the deploy failed"
    fi
    die "$ROLLBACK_FAILED"
  fi
fi

# The worker and the scheduler are recreated in place: tasks queue in Redis
# while they restart, and two schedulers side by side would fire every cron
# twice. When they never come up the whole stack goes back to the previous
# image, so no two versions of the code keep running together. The migrations
# stay applied: the previous code runs against the new schema.
echo "[deploy] replacing the worker and the scheduler"
if ! replace_worker_and_scheduler; then
  if [ -z "$PREVIOUS_IMAGE" ]; then
    die "[deploy] the worker or the scheduler did not become healthy and there is no previous image to go back to"
  fi
  echo "[deploy] the worker or the scheduler did not become healthy; going back to ${PREVIOUS_IMAGE}"
  # Every step is tried even when an earlier one failed: the bot must not stay
  # on the new image beside a worker that went back.
  rollback_failed=0
  BOT_IMAGE="$PREVIOUS_IMAGE" replace_worker_and_scheduler || rollback_failed=1
  if [ "$BOT_MODE" = "webhook" ]; then
    roll_bot "$PREVIOUS_IMAGE" || rollback_failed=1
  else
    BOT_IMAGE="$PREVIOUS_IMAGE" replace_polling_bot || rollback_failed=1
  fi
  if [ "$rollback_failed" = 0 ]; then
    die "[deploy] rolled back; the deploy failed"
  fi
  die "$ROLLBACK_FAILED"
fi

# Every BUILD=0 deploy leaves a tagged sha- image behind, and plain
# `image prune` only touches dangling ones, so the disk grows until a pull
# fails. A week keeps enough recent tags to go back to. Only on the box: on a
# developer's machine it would remove every other project's images too.
if [ "$PRUNE" != "1" ]; then
  echo "[deploy] PRUNE=${PRUNE}: keeping unused images and build cache"
elif [ "$(uname -s)" != "Linux" ]; then
  echo "[deploy] not on Linux: keeping unused images and build cache"
else
  echo "[deploy] pruning images and build cache unused for a week"
  docker image prune -af --filter "until=168h"
  docker builder prune -f --filter "until=168h"
fi

echo "[deploy] done"
"${COMPOSE[@]}" ps
