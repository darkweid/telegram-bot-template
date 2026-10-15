# Configuration

Every key the template reads from `.env`, with its default and what goes
wrong when it is set badly. Open this document when you fill in `.env` for a
new bot or a new server, or when the bot refuses to start over a setting.
`.env.example` holds the same keys with a comment each; copy it to `.env` and
replace every value that carries `not-real`.

## How settings are read

`get_settings()` in `src/main/config.py` builds one settings class per section
below and gathers them in `Config`. It reads `.env`, or `.env.test` when the
environment variable `TESTING` is `true`, and a variable set in the
environment wins over the file. Compose hands `.env` to the containers through
`env_file`, so inside the stack every key arrives as an environment variable.
Keys a section does not know are ignored.

A wrong value stops the process at startup with the name of the key and the
rule it broke. Rules that span sections live on `Config`:

- `USER_CACHE_TTL_SECONDS` may not exceed `CACHE_VERSION_TTL`;
- `LLM_LOG_CONTENT=true` requires `DEBUG=true`;
- `BOT_MODE=webhook` requires an `https` `WEBHOOK_BASE_URL` without a query
  or fragment, and a `WEBHOOK_SECRET`.

In the tables, "required" means the key has no default and startup fails
without it; "empty" means the key may stay blank.

## App

| Key | Default | What it does |
|---|---|---|
| `PROJECT_NAME` | required | Display name in `/status`, `/test_alert` and the default HTTP user agent. |
| `VERSION` | required | Release shown in `/health` and `/status` and sent to Sentry as the release; a stale value mislabels every error. |
| `DEBUG` | `false` | Turns Sentry off and allows `LLM_LOG_CONTENT`. Left on in production, errors never reach Sentry. |
| `TESTING` | `false` | Turns Sentry off. As an environment variable, `true` also makes the settings come from `.env.test`. |
| `LOG_LEVEL` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` or `DEBUG`, read by `loggers/` from the environment. `DEBUG` in production floods the logs. |
| `LOG_JSON` | `false` | One JSON object per log line instead of text, for log aggregators that parse stdout. |

## Bot

| Key | Default | What it does |
|---|---|---|
| `BOT_TOKEN` | required | The token from @BotFather; startup refuses a value that does not look like one. A token another process polls makes both lose updates. |
| `BOT_MODE` | `polling` | `polling` or `webhook`. Webhook mode needs the [webhook](#webhook) keys and the `webhook` compose profile. |
| `DEFAULT_LOCALE` | `en` | Language for users whose client reports none or one without a catalog; must be a supported locale. |
| `USER_TOUCH_INTERVAL_SECONDS` | `300` | A user row is rewritten when the profile changes or `last_seen_at` is older than this. Too low writes on almost every update. |
| `USER_CACHE_TTL_SECONDS` | `300` | How long a cached user snapshot answers updates without the database; a row edited by hand shows after at most this long. Must not exceed `CACHE_VERSION_TTL`. |
| `TELEGRAM_RETRY_ATTEMPTS` | `3` | Flood-wait retries per Bot API call, 0 to 10; all calls of one update share 30 seconds of sleep. |
| `SHUTDOWN_TIMEOUT_SECONDS` | `20` | How long a stopping bot finishes updates in progress and then its background work, in one budget. Keep it at least 5 seconds below `BOT_STOP_GRACE_PERIOD`, or Docker kills the bot mid-update; `make check-env` refuses less. |
| `ADMIN_IDS` | empty | Comma-separated Telegram user ids with `/stats`, `/ban`, `/unban`, `/broadcast` and the new-user notice. A group id is refused; a wrong id grants the role to nobody or to a stranger. |
| `DEVELOPER_IDS` | empty | Comma-separated user ids with `/status`, `/test_alert` and the developer alerts. |
| `DEV_ALERT_CHAT_ID` | empty | A chat that gets every developer alert; empty sends each alert to every developer in private. `0` is refused. The bot must be a member of the chat, or alerts are only logged. |
| `ALERT_COOLDOWN_SECONDS` | `300` | The same failure is alerted once per this many seconds. Too low floods the alert chat during an incident; too high hides a recurrence. |
| `NOTIFY_ADMINS_ON_NEW_USER` | `true` | Messages every admin when a user starts the bot for the first time; `false` keeps the task but sends nothing. |

## Throttling

How many handler calls one user gets per window. Over the limit the bot warns
once and drops the rest until the window ends. Admins and developers are never
limited.

| Key | Default | What it does |
|---|---|---|
| `THROTTLE_DEFAULT_LIMIT` | `20` | Calls per window for ordinary handlers. Too low and users hit it while tapping through menus. |
| `THROTTLE_DEFAULT_WINDOW_SECONDS` | `60` | The window of the default limit. |
| `THROTTLE_LLM_LIMIT` | `5` | Calls per window for handlers flagged `Throttle.LLM`, the ones that pay for a model call. |
| `THROTTLE_LLM_WINDOW_SECONDS` | `60` | The window of the LLM limit. |

## Broadcast

| Key | Default | What it does |
|---|---|---|
| `BROADCAST_RATE_PER_SECOND` | `25` | Copies sent per second, above 0 and at most 30, the rate Telegram allows to different users. |
| `BROADCAST_CHECKPOINT_EVERY` | `50` | Recipients between checkpoints, 1 to 1000. After a worker restart up to this many users may get the message twice. |

## Web server

| Key | Default | What it does |
|---|---|---|
| `WEB_SERVER_HOST` | `0.0.0.0` | Interface of the health endpoints and, in webhook mode, the webhook route. A loopback address hides the webhook from nginx, which runs in another container. |
| `WEB_SERVER_PORT` | `8080` | Port of the same server; the compose healthcheck calls `/live` on it. |

## Webhook

Read only when `BOT_MODE=webhook`; see [the deployment guide](deployment.md#webhook-mode).

| Key | Default | What it does |
|---|---|---|
| `WEBHOOK_BASE_URL` | empty | The public `https` origin Telegram calls. Webhook mode refuses to start without it. |
| `WEBHOOK_PATH` | `/webhook/telegram` | Path of the webhook route: segments of letters, digits, `_` and `-`, at most 256 characters. |
| `WEBHOOK_SECRET` | empty | 32 to 256 characters of `A-Z a-z 0-9 _ -` that Telegram echoes on every update; a request without it is rejected. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. |
| `WEBHOOK_MAX_CONNECTIONS` | `40` | Simultaneous connections Telegram may open, 1 to 100. |

## PostgreSQL

| Key | Default | What it does |
|---|---|---|
| `DB_ECHO` | required | Logs every SQL statement when true; keep it off outside local debugging. |
| `POSTGRES_USER` | required | Database user; the `postgres` container creates it on first start. |
| `POSTGRES_PASSWORD` | required | Its password, also set on first start; changing it later does not change the stored one. |
| `POSTGRES_HOST` | required | `postgres` inside the stack, `localhost` for a bot run on the host against the published port. |
| `POSTGRES_PORT` | required | Port the bot dials, 5432 inside the stack. |
| `POSTGRES_DB` | required | Database name; a name with `?` is refused, since a database URL cannot carry it. |
| `DB_POOL_SIZE` | `5` | Pooled connections per process. |
| `DB_MAX_OVERFLOW` | `2` | Extra connections opened above the pool under load. |
| `DB_STATEMENT_TIMEOUT_SECONDS` | `30` | Server-side cap on one statement; `0` turns it off. A transaction may raise its own cap. |
| `DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS` | `60` | The server closes a connection that sits idle inside a transaction this long; `0` turns it off. |

## Redis

Each role has its own database, so flushing the cache never drops a dialog or
a queued task. The three indexes must differ, compared as numbers (`01` is
`1`); the bot, the worker and the scheduler refuse to start otherwise.

| Key | Default | What it does |
|---|---|---|
| `REDIS_HOST` | required | `redis` inside the stack. |
| `REDIS_PORT` | required | Port the bot dials, 6379 inside the stack. |
| `REDIS_PASSWORD` | required | Password the `redis` container is started with and every client sends. |
| `REDIS_DATABASE` | required | Database of the cache, the rate limiters and the alert cooldown. |
| `REDIS_FSM_DATABASE` | `2` | Database of the dialog (FSM) state. |
| `REDIS_TASKS_DATABASE` | `1` | Database of the task queue, the scheduled retries and the scheduler heartbeat. |

## Cache

| Key | Default | What it does |
|---|---|---|
| `CACHE_ENABLED` | `true` | `false` makes every read a miss; nothing breaks, the database takes every read. |
| `CACHE_DEFAULT_TTL` | `60` | Seconds a value lives unless the caller sets its own; may not exceed `CACHE_VERSION_TTL`. |
| `CACHE_VERSION_TTL` | `604800` | Seconds a namespace's version counter lives. No entry may outlive it, or a value could be served after its invalidation. |
| `CACHE_KEY_PREFIX` | `cache` | Prefix of every cache key; change it when two bots share one Redis database. |

## Sentry

| Key | Default | What it does |
|---|---|---|
| `SENTRY_DSN` | empty | Project DSN; empty keeps Sentry off. |
| `SENTRY_ENV` | `development` | Environment name on every event, so staging and production errors stay apart. |
| `SENTRY_ENABLED` | `false` | Turns Sentry on together with a DSN. `DEBUG` or `TESTING` keeps it off whatever this says. |

## HTTP

| Key | Default | What it does |
|---|---|---|
| `HTTP_USER_AGENT` | empty | User-Agent of requests to third-party APIs, printable ASCII up to 256 characters. Empty sends `<project-name>/<VERSION>` built from `PROJECT_NAME`; some APIs refuse a request without a recognizable agent. |

## LLM

The layer itself is described in [the LLM guide](llm.md).

| Key | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | empty | `anthropic` or `openrouter`. Empty leaves the layer off: every LLM feature answers "not available". |
| `LLM_MODEL` | `claude-sonnet-5-5` | Model of the primary provider. Anthropic ids as they are, OpenRouter ids as `<vendor>/<model>`; a wrong shape is refused. |
| `LLM_MODEL_FAST` | empty | Model of requests marked `ModelTier.FAST`, same provider and id shape as `LLM_MODEL`; empty means `LLM_MODEL`. `.env.example` sets `claude-haiku-5-5`. |
| `LLM_FALLBACK_PROVIDER` | empty | The other provider, tried when the primary is unavailable, times out or is rate limited. The same provider twice, or a fallback without a primary, is refused. |
| `LLM_FALLBACK_MODEL` | empty | Model of the fallback provider; required with it. |
| `LLM_FALLBACK_MODEL_FAST` | empty | The fallback's model for `ModelTier.FAST`; empty means `LLM_FALLBACK_MODEL`. |
| `ANTHROPIC_API_KEY` | empty | Required when Anthropic is the primary or the fallback. |
| `OPENROUTER_API_KEY` | empty | Required when OpenRouter is the primary or the fallback. |
| `OPENROUTER_APP_URL` | empty | Public URL OpenRouter credits the calls to (`HTTP-Referer`); printable ASCII, blank sends nothing. |
| `OPENROUTER_APP_TITLE` | empty | App name OpenRouter credits the calls to (`X-Title`); printable ASCII, blank sends nothing. |
| `LLM_TIMEOUT_SECONDS` | `60` | Seconds per attempt, at most 600. A user waits up to timeout x (retries + 1) before the fallback runs. |
| `LLM_MAX_RETRIES` | `1` | Retries after a 429, a 5xx or a network error, 0 to 5. |
| `LLM_PRICING_OVERRIDES` | empty | JSON that replaces or extends the price table in `src/core/llm/pricing.py`; each entry needs all four rates. Malformed JSON or a bad rate stops startup. |
| `LLM_USAGE_RETENTION_DAYS` | `90` | Days of `llm_usage` rows the daily purge keeps; at least 30, the longest period `/stats` reports. |
| `LLM_LOG_CONTENT` | `false` | Logs every prompt and answer, which carry what users typed. Refused unless `DEBUG=true`, and refused by `make check-env`. |

## S3

Off by default: the S3 client then refuses every call with `S3Disabled`, and
no other key here is needed.

| Key | Default | What it does |
|---|---|---|
| `S3_ENABLED` | `false` | Turns file storage on; startup then requires the bucket, both keys and the region. |
| `S3_BUCKET_NAME` | empty | Bucket every call uses unless it names another; a wrong name fails each request with `NoSuchBucket`. |
| `S3_ACCESS_KEY_ID` | empty | Access key id. |
| `S3_SECRET_ACCESS_KEY` | empty | Secret access key. |
| `S3_REGION_NAME` | empty | Region of the bucket. A wrong one makes AWS answer with a redirect or a signature error; S3-compatible services usually accept any value. |
| `S3_PRE_SIGNED_URL_SECONDS` | `300` | Lifetime of a presigned URL unless the caller passes its own. |
| `S3_ENDPOINT_URL` | empty | Blank for AWS; set for MinIO, R2 and the like. An endpoint switches addressing to `path` unless `S3_ADDRESSING_STYLE` says otherwise. |
| `S3_ADDRESSING_STYLE` | empty | `auto`, `path` or `virtual`; blank lets the client choose. |
| `S3_SIGNATURE_VERSION` | `s3v4` | Request signing scheme; an older one is refused by most services and every newer AWS region. |
| `S3_VERIFY_SSL` | `true` | `false` only for a self-hosted endpoint with its own CA; against AWS it lets anyone on the path read the keys and the files. |
| `S3_CA_BUNDLE` | empty | CA bundle file for the endpoint's certificate; blank uses the system store. A wrong path fails every request. |
| `S3_TREAT_ACCESS_DENIED_AS_MISSING` | `false` | `true` when the credentials may not read missing keys: `head_object` then reports a 403 as a missing object. |
| `S3_CONNECT_TIMEOUT_SECONDS` | `5` | Seconds to open a connection. |
| `S3_READ_TIMEOUT_SECONDS` | `120` | Seconds to wait for data. With retries one call can outlast the 60s event-isolation lock: move large transfers to a task. |
| `S3_RETRY_MAX_ATTEMPTS` | `3` | Attempts per request, the first included; `1` turns retries off. |
| `S3_RETRY_MODE` | `standard` | botocore retry mode: `legacy`, `standard` or `adaptive`. |
| `S3_MAX_UPLOAD_SIZE_BYTES` | `20971520` | Largest stream `upload_stream` accepts (20 MiB, what the Bot API lets a bot download); over it the upload fails with `S3ObjectTooLarge`. |

## Email

Outgoing mail over SMTP, sent by the worker. Off by default: the mailer refuses
every send and `EmailService` refuses to queue one.

| Key | Default | What it does |
|---|---|---|
| `EMAIL_ENABLED` | `false` | Turns email on; startup then requires the server, the user and the sender name. |
| `EMAIL_SERVER` | empty | SMTP host. |
| `EMAIL_PORT` | `587` | 587 with `EMAIL_STARTTLS`, 465 with `EMAIL_USE_TLS`. |
| `EMAIL_USER` | empty | The login and the From address. |
| `EMAIL_PASSWORD` | empty | Empty skips authentication, which only a local catcher such as Mailpit accepts. |
| `EMAIL_FROM_NAME` | empty | Display name in From and in the footer of every template. |
| `EMAIL_USE_TLS` | `false` | TLS from the first byte (port 465). Both this and `EMAIL_STARTTLS` true is refused. |
| `EMAIL_STARTTLS` | `true` | Upgrades the connection (port 587). Both TLS keys false sends in plain text, for a local catcher only. |
| `EMAIL_VALIDATE_CERTS` | `true` | `false` only for a local catcher with a self-signed certificate; against a real server it exposes the password and the mail. |

## Compose and resource limits

These keys are read by compose and nginx, not by the bot. An empty value
falls back to the default written in `infra/docker-compose.yml`.

| Key | Default | What it does |
|---|---|---|
| `COMPOSE_PROFILES` | empty | `webhook` adds nginx and certbot. Set it exactly when `BOT_MODE=webhook`; `make check-env` and the deploy refuse a mismatch. |
| `TELEGRAM_ALLOWED_SUBNETS` | `149.154.160.0/20,91.108.4.0/22` | Networks nginx accepts webhook requests from; any other address is dropped without an answer. A missing subnet loses the updates Telegram sends from it. |
| `BOT_STOP_GRACE_PERIOD` | `30s` | How long Docker waits for a stopping bot before it kills it (the bot's `stop_grace_period`), in whole seconds such as `30s`. Must be at least `SHUTDOWN_TIMEOUT_SECONDS` plus 5 seconds: the margin covers recording a cancelled model call and editing a status to "interrupted". A polling deploy takes no updates while the old bot stops. |
| `BOT_IMAGE` | `telegram-bot-template-bot:latest` | Image of `migrate`, `bot`, `worker` and `scheduler`. Empty means the local tag built from the checkout; an image CI pushed (`ghcr.io/<owner>/<repo>:sha-<12>`) is pulled instead, and the bot image is not built. |
| `POSTGRES_HOST_PORT` | `5432` | Host side of PostgreSQL's `127.0.0.1` bind. Change it only when the host port is taken; `POSTGRES_PORT` stays 5432. |
| `REDIS_HOST_PORT` | `6379` | Host side of Redis's `127.0.0.1` bind. |
| `POSTGRES_CPUS` | `1` | CPU cap of `postgres`. |
| `POSTGRES_MEMORY` | `1G` | Memory cap of `postgres`; keep it above `shared_buffers` (256MB) plus `work_mem` for the busiest connections. |
| `MIGRATE_CPUS` | `1` | CPU cap of the one-shot `migrate` service. |
| `MIGRATE_MEMORY` | `256M` | Memory cap of `migrate`. |
| `BOT_CPUS` | `1` | CPU cap of `bot`. |
| `BOT_MEMORY` | `512M` | Memory cap of `bot`. |
| `WORKER_CPUS` | `1` | CPU cap of `worker`. |
| `WORKER_MEMORY` | `512M` | Memory cap of `worker`. |
| `SCHEDULER_CPUS` | `0.5` | CPU cap of `scheduler`. |
| `SCHEDULER_MEMORY` | `256M` | Memory cap of `scheduler`. |
| `REDIS_CPUS` | `1` | CPU cap of `redis`. |
| `REDIS_MEMORY` | `1G` | Memory cap of `redis`; keep it at least 1.5 times `maxmemory` in `infra/redis.conf` (512mb). |
| `NGINX_CPUS` | `0.5` | CPU cap of `nginx`. |
| `NGINX_MEMORY` | `128M` | Memory cap of `nginx`. |
| `CERTBOT_CPUS` | `0.5` | CPU cap of `certbot`. |
| `CERTBOT_MEMORY` | `256M` | Memory cap of `certbot`. |

The limits are caps, not reservations: the defaults may add up to more than a
small server has, and a container past its memory cap is killed by the kernel.

## Commit

`COMMIT_SHA` is the commit an image was built from, shown in `/health`,
`/status` and `/test_alert`. It is a build argument, never a line in `.env`:
`env_file` would put it into every container over the image's own value.
An image built through `make` gets `git rev-parse --short=12 HEAD` (a value
from the shell or the command line wins), CI passes the same 12 characters it
puts in the image's `sha-` tag, and an image built without it reports
`unknown`, as the dev stack does.

## Checking .env before a deploy

`make check-env` runs `python3 scripts/ops/check_env.py .env`. It uses the
standard library only, so it runs with the server's system Python, and
`infra/deploy/deploy.sh` runs it before anything else. It reports what the
bot's own config cannot see, or would only report once a container
crash-loops. Errors (exit 1):

- a `BOT_TOKEN` that does not look like a token, a `BOT_MODE` other than
  `polling` or `webhook`;
- in webhook mode: a `WEBHOOK_BASE_URL` that is not an `https` origin (a path,
  query, fragment or a port other than 443 is refused), a malformed
  `WEBHOOK_PATH` or `WEBHOOK_SECRET`, a loopback `WEB_SERVER_HOST`;
- `COMPOSE_PROFILES` without `webhook` in webhook mode, or with it in polling
  mode;
- malformed `TELEGRAM_ALLOWED_SUBNETS`, role ids that are not positive, a
  zero or non-numeric `DEV_ALERT_CHAT_ID`;
- `LLM_LOG_CONTENT` turned on, or a `COMMIT_SHA` line;
- a host port that is not a port number, a `*_CPUS` or `*_MEMORY` value of
  the wrong shape;
- a value that still carries the `not-real` placeholder, unless its feature
  is off (Sentry, S3 and email keys while their switch is off, the key of a
  provider no `LLM_PROVIDER` names).

A key the template does not read is a warning: a misspelled key leaves the
default in place. Every message names the key and never the value, since the
output lands in CI logs.
