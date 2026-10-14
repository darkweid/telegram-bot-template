# tg-bot-template

A production-ready template for Telegram bots on aiogram 3. One codebase runs
in polling or webhook mode, chosen by a single setting. PostgreSQL stores the
users and Redis keeps the dialog (FSM) state. Dependencies are wired with
dishka, from the database session down to the use case a handler calls.
English and Russian work out of the box, and adding a language is a catalog
and two lines of code. Unit tests run against a fake Bot API, integration
tests against a real PostgreSQL and Redis, and GitHub Actions runs both on
pull requests.

## Features

- Polling and webhook modes with graceful shutdown: on SIGTERM the bot stops
  taking updates and gives the ones in progress `SHUTDOWN_TIMEOUT_SECONDS` to
  finish.
- `/start`, `/help` and `/language`, with a per-user language and a command
  menu published in every supported language.
- Users stored on their first private-chat update and kept fresh with
  minimal writes: a row is rewritten only when the Telegram profile changes
  or once per `USER_TOUCH_INTERVAL_SECONDS`.
  A snapshot of the stored user is cached in Redis for
  `USER_CACHE_TTL_SECONDS`, so most updates read no row at all.
- Tracking of users the bot can no longer reach (they blocked the bot or
  deleted their account) in `is_reachable` and `unreachable_since`, for code
  that messages users unprompted. A user becomes reachable again with their
  next update or when they unblock the bot.
- Flood-control retries: a Bot API call that hits `RetryAfter` waits as long
  as Telegram asks and tries again, up to `TELEGRAM_RETRY_ATTEMPTS` times. All
  calls of one update share 30 seconds of waiting.
- Roles from configuration: admins (`ADMIN_IDS`) ban and unban users, see
  user statistics and send broadcasts; developers (`DEVELOPER_IDS`) get alerts
  and `/status`. Each role sees its own command menu.
- Per-user throttling with named profiles (`THROTTLE_*`); admins and
  developers are not limited.
- Background tasks on taskiq over a Redis stream: a `worker` and a single
  `scheduler`. A use case queues a task through a transactional outbox, so a
  task runs only for work that was committed.
- Admins are told when a new user starts the bot
  (`NOTIFY_ADMINS_ON_NEW_USER`).
- Developer alerts in Telegram for unexpected errors, tasks that failed for
  good and Redis degradation, once per `ALERT_COOLDOWN_SECONDS` for the same
  error.
- Broadcasts to every reachable user, paced under Telegram's limit, with live
  progress and a Stop button, resumed from the last checkpoint after a worker
  restart.
- A global error handler that logs the failure, answers the user in their
  Telegram language, and reports unexpected errors to Sentry.
- `/live`, `/ready` and `/health` probes in both modes; `/health` also reports
  the outbox backlog, the task queue and the scheduler heartbeat.
- Strict typing (mypy), import-layer contracts (import-linter), a fake Bot
  API for tests and a 90% coverage gate.
- GitHub Actions CI on pull requests and pushes to `main`: pre-commit hooks,
  a single Alembic head, unit tests with coverage, integration tests against
  PostgreSQL and Redis, Bandit, pip-audit and Gitleaks. A push to `main` that
  passes them builds the image and pushes it to GHCR, tagged `latest` and
  `sha-` plus the first 12 characters of the commit. A change that touches
  only documentation runs the lint and Gitleaks jobs alone.

## Architecture

```mermaid
flowchart LR
    TG[Telegram] -->|getUpdates or webhook| DP[Dispatcher]
    DP --> MW["Middlewares<br/>context · dishka · user · locale · ban · throttling"]
    MW --> H[Handlers]
    H --> UC[Use cases]
    UC --> R[Repositories]
    R --> PG[("PostgreSQL")]
    DP --> FSM[("Redis<br/>FSM state")]
    H -->|replies| S["Bot API session<br/>flood retries · reachability"]
    S --> TG
    UC -->|outbox row| R
    PG -->|after commit or sweep| Q[("Redis<br/>task stream")]
    SC[Scheduler] --> Q
    Q --> W[Worker]
    W --> UC
    W --> S
```

Handlers are thin and get their use cases from the dishka container. A use
case owns the transaction through a unit of work. Repositories hold all SQL.

Background work follows the same path: the worker runs a task through a use
case from the same container, and the scheduler only queues tasks.

## Quick start (polling)

You need Docker with Compose v2, Python 3.13 for the tooling, and a bot token
from [@BotFather](https://t.me/BotFather).

```bash
git clone https://github.com/darkweid/tg-bot-template.git my-bot && cd my-bot
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r infra/requirements/dev.txt
cp .env.example .env          # set BOT_TOKEN
make run-dev                  # compiles catalogs, migrates, starts with autoreload
make logs s=bot
```

The dev stack always polls. A change to a `.py` file under `src/`,
`loggers/` or `models/` restarts the bot. After editing a `.po` file, run
`make i18n-compile restart s=bot`.

## Webhook mode

Set three variables in `.env`:

- `BOT_MODE=webhook`;
- `WEBHOOK_BASE_URL`, the public HTTPS origin Telegram will call;
- `WEBHOOK_SECRET`, 32 to 256 letters, digits, `_` or `-`, which Telegram
  echoes on every update. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

The bot refuses to start in webhook mode without an `https` base URL and a
secret. It sets the webhook on every start and keeps it on shutdown, so
Telegram queues updates while the next container starts.

Run the prod-like stack with `make run`. A one-shot `migrate` service applies
the migrations baked into the image before the bot starts, and the bot waits
for it; `make migrate` runs the same service by hand. With `BOT_IMAGE` set in
`.env` to an image CI pushed (`ghcr.io/<owner>/<repo>:sha-<12>`), `make run`
pulls and runs that image instead of building one from the checkout.

Put a reverse proxy with a valid certificate in front of the bot and forward
`WEBHOOK_PATH` (default `/webhook/telegram`) to `bot:${WEB_SERVER_PORT}` on the
`tg-bot-template-network` Docker network. The compose file publishes no port
of the bot.

## Roles

Two optional roles, set in `.env` as comma-separated Telegram user ids:

- `ADMIN_IDS`: admins get the admin commands;
- `DEVELOPER_IDS`: developers get the developer commands: `/status` shows the
  `/health` report, `/test_alert` sends a test alert the way real alerts go,
  past the cooldown.

Neither is throttled, and a ban never drops their updates (see below for
broadcasts and statistics). Each listed id gets a command menu of its own in
its private chat: the user commands plus those of its roles. An id that never
opened the bot cannot get a menu yet; the bot logs one warning and starts
anyway, and the menu is set on the next start after that user opens the bot.

Admins ban and unban users with `/ban` and `/unban`, followed by a Telegram
user id or `@username` (matched ignoring case; a username that several stored
users have had is refused, use the id). Only users who have started the bot
can be named. Admins and developers cannot be banned, and nobody bans
themselves. A banned user's updates are dropped without an answer. A ban or
unban made while Redis is down holds during the outage, when every update
reads the stored row; the snapshot cached before it could undo it once Redis
is back, so the worker drops that snapshot then, usually within a couple of
minutes. A role beats a ban for updates only: someone banned before their id
was added to `ADMIN_IDS` or `DEVELOPER_IDS` is served as usual, but
broadcasts skip them and `/stats` counts them as banned until they are
unbanned with `/unban`.

Admins also get `/stats`: users in total, reachable, banned, and active and
new in the last 1, 7 and 30 days.

The role menus are set for each listed id on every start. Removing an id from
`ADMIN_IDS` or `DEVELOPER_IDS` takes the role away at once, but Telegram keeps
showing that user the old menu: the bot never deletes a menu it set. Clear it
once by hand with `deleteMyCommands` for the scope
`{"type": "chat", "chat_id": <id>}` and every language code the bot set.

## Throttling

Each user gets `THROTTLE_DEFAULT_LIMIT` handler calls per
`THROTTLE_DEFAULT_WINDOW_SECONDS` (20 per 60 seconds). Over the limit the bot
warns once per window and drops the rest. A handler that calls a paid model
takes the stricter profile with `flags={"throttle": Throttle.LLM}`, limited by
`THROTTLE_LLM_LIMIT` and `THROTTLE_LLM_WINDOW_SECONDS` (5 per 60 seconds).
Admins and developers are never limited. Counters live in Redis; while it is
down each instance counts in memory, so the limit stays.

## Background tasks

Background work runs on [taskiq](https://taskiq-python.github.io/) over Redis
Streams, in two more services of the same image:

- `worker` runs the tasks: one process, up to 20 tasks at once. Scale it with
  replicas. It waits for the migrations like the bot, and a delivery of a task
  that already completed or is running elsewhere is skipped.
- `scheduler` fires cron schedules and the worker's delayed retries. Run
  exactly one: taskiq has no duplicate-fire protection, so two schedulers fire
  every cron twice. Its healthcheck reads a heartbeat the scheduler loop
  writes every 30 seconds.

The queue lives in its own Redis database, `REDIS_TASKS_DATABASE` (default
`1`), which must differ from `REDIS_DATABASE` and `REDIS_FSM_DATABASE`; the
configuration refuses two roles on one database at startup.
`make run` and `make run-dev` start both services; `make logs s=worker` and
`make logs s=scheduler` show their output.

In the dev stack the worker restarts on a change to a `.py` file under `src/`,
`loggers/` or `models/`, like the bot. The scheduler does not run under
watchfiles: after changing a schedule, run `make restart s=scheduler`.

A use case enqueues a task inside its unit of work:

```python
async with self._uow as uow:
    ...
    await uow.enqueue_task("reports.send", user_id=user.id)
    await uow.commit()
```

The task is stored as a row in `outbox_messages` in the same transaction, so it
exists only if the work that caused it commits. Arguments must be plain JSON
values (ids, strings, numbers, lists, dicts); anything a JSON round-trip would
change is refused. Right after commit the bot sends the task to the queue. If
that send fails, the `outbox.sweep` task, which runs every minute, sends the row
within a minute or two; the row id is the task id, so a row sent twice runs
once. `outbox.purge` deletes published rows older than
`PUBLISHED_RETENTION_DAYS` (7) every day at 03:00 UTC.

Every task is registered in one place: a `TaskSpec` in `TASKS`
(`src/main/tasks.py`), with its name, function, optional cron schedule and
retry flag. A name missing there is never run, and the sweeper marks its rows
failed.

A domain event is the other way to start a task. A use case publishes it
inside its unit of work (`uow.publish`); the event's subscribers, mapped in
`src/main/event_subscribers.py`, run in that same transaction and enqueue
tasks. The reference path is the new-user notice: `TouchUserUseCase` publishes
`UserRegistered` when the upsert inserted the user, `enqueue_new_user_notice`
stores `user.notify_admins_of_new_user` in the outbox, and the worker sends
every admin in `ADMIN_IDS` who has opened the bot a message in that admin's
language. The task is not retried: a retry would repeat the message to the
admins who already got it. `NOTIFY_ADMINS_ON_NEW_USER=false` keeps the event
and the task and sends nothing.

## Alerts

The bot and the worker tell developers in Telegram when:

- a handler raises an unexpected error (a `CoreException` is an expected
  refusal and alerts nobody);
- a task fails for good, after its last retry;
- Redis goes down under the cache or the throttle limiter, and again when it
  recovers.

Alerts go to `DEV_ALERT_CHAT_ID`, a group or channel id, when it is set, and
otherwise to each of `DEVELOPER_IDS` in private. The bot must be a member of
the alert chat, and a developer must have opened the bot: an alert Telegram
refuses is logged and nothing more.

An error alert carries the error type, where it was raised, what was being
handled (`update message`, `task <name>`), the user id or task id, and the
Sentry event id when Sentry is on. The error message is shown (cut to 500
characters) only for a `CoreException`, whose message the project writes, and
for a driver or validation error, whose message is rebuilt from safe fields;
any other message can quote what the user sent (`int(message.text)`), so the
alert says it is in the logs (and in Sentry, when on) instead. It never
carries what the user sent or their name. The same failure (type, place and
origin) is alerted once per `ALERT_COOLDOWN_SECONDS` (300), and a task once
however often it is redelivered.

## Broadcast

`/broadcast` (admins) sends one message to every user who can receive it:
reachable and not banned.

1. Send `/broadcast`, then the message: text, a photo or video with a caption,
   a document. Albums are refused. The bot shows an exact copy and the number of
   recipients.
2. Press Send. The bot copies the message to every recipient and keeps a
   progress message with a Stop button, updated at most every 5 seconds. When
   the broadcast ends, the progress message turns into the report and the bot
   sends the report again, so the admin gets a notification.

Only one broadcast runs at a time. It goes to the users the bot had when Send
was pressed; someone who starts the bot later does not get it. Do not delete
the original message until the broadcast has finished: every copy is made from
it, and if it is gone, the broadcast stops as failed and developers are
alerted.

The total in the progress message is the number of recipients when Send was
pressed. Users are walked in order of their id, so a change mid-run counts only
for users the walk has not reached yet: one who unblocks the bot is reached
too, one who blocks it or is banned is skipped; the
progress never reads past its total, and the final report of a completed
broadcast counts the users who were actually walked.

The worker sends `BROADCAST_RATE_PER_SECOND` messages a second (25 by default;
Telegram allows about 30 to different users) and saves its position every
`BROADCAST_CHECKPOINT_EVERY` recipients (50) and at least once a minute. If the
worker stops mid-broadcast, a scheduled task resumes it within about five
minutes from the last saved position, so up to `BROADCAST_CHECKPOINT_EVERY`
users may get the message twice. Stop takes effect at the next save, within
about a minute, flood waits included.

A user who blocked the bot is counted as unreachable and marked so; a chat
Telegram cannot find counts as failed. Network errors are retried. Anything
else marks the broadcast failed, sends the report and alerts developers.

## Health

The bot serves three probes on `WEB_SERVER_PORT` in both modes, on the compose
network only:

- `/live` answers 200 while the process runs; the Docker healthcheck reads it.
- `/ready` answers 200 when PostgreSQL and every Redis database (cache, FSM
  state, task queue) answer, and 503 otherwise.
- `/health` always answers 200 with a JSON report: `status` (`ok` or
  `degraded`), `version`, `commit`, `mode`, `postgres`, `redis`,
  `redis_memory_used_ratio`, `outbox_pending`, `outbox_oldest_pending_seconds`,
  `task_queue_length` and `scheduler_heartbeat_age_seconds`. A reading that
  failed or timed out is `null`, and so is every Redis reading while any
  Redis database is down.

`task_queue_length` is the workers' backlog: tasks their consumer group has
not delivered yet plus those delivered and not acknowledged. It is not the
stream's length, which keeps acknowledged tasks until the stream is trimmed.
It is `null` when Redis cannot count the undelivered ones, which happens after
some trims and deletions.

`/health` reports `degraded` when PostgreSQL or Redis is down, when Redis is at
90% of `maxmemory`, when the scheduler has not beaten for more than 90 seconds
(no cron task, delayed retry or outbox sweep runs without it), or when an
outbox row has waited for publishing longer than 5 minutes. The task queue
never changes the status: there is no backlog that is wrong for every bot.
Developers see the same report with `/status`.

## Project layout

```text
.github/            CI workflows and the docs-only change action
infra/              Dockerfiles, compose files, Postgres and Redis config, requirements
locales/            gettext catalogs, one directory per language
loggers/            logging setup: plain-text or JSON lines on stdout
migrations/         Alembic environment and revisions
models/             every SQLAlchemy model, imported for Alembic autogenerate
scripts/ops/        helpers behind make targets and pre-commit hooks
src/admin/          admin commands: ban, unban, statistics
src/broadcast/      broadcasts: model, use cases, tasks, the draft dialog
src/core/           shared infrastructure: database, outbox, tasks, Redis, cache,
                    rate limits, HTTP client, errors, i18n, Telegram helpers
src/developer/      developer commands: status, test alert
src/main/           entry points (bot, worker, scheduler), config, dependency
                    container, dispatcher, polling and webhook
src/system/         health checks behind the probes and /status
src/user/           users: handlers, keyboards, middleware, use cases, repository
tests/unit/         tests that need no Docker
tests/integration/  tests against a throwaway PostgreSQL and Redis
tests/fakes/        the fake Bot API session, database and Redis fakes
tests/factories/    Telegram updates and stored users for tests
tests/helpers/      a running bot on the fake API, provider overrides, log capture
```

## Development

```bash
pre-commit install            # run the hooks on every commit
make help                     # every target with a one-line description
make lint                     # every pre-commit hook over every file
make test                     # unit tests, no Docker needed
make test-cov                 # unit tests with a coverage report
make test-integration         # integration tests (needs Docker)
make i18n-update              # merge new and changed messages into the catalogs
make migration m="add something"  # new Alembic revision, autogenerated
```

`make migration` runs in the dev image as your user: it starts Postgres,
migrates it to head and diffs the models against it. Autogenerate misses
renames and some type and server-default changes: read the revision before
committing it.

`make` passes the checkout's commit (`git rev-parse --short=12 HEAD`) to the
image build as `COMMIT_SHA`, and CI passes the same 12 characters it puts in
the `sha-` tag; `/status` and `/health` show it, `unknown` without one. The
dev stack (`make run-dev`) runs the live checkout rather than a baked image, so
it always shows `unknown`. Leave
`COMMIT_SHA` out of `.env`: the compose services read that file, and a value
there would override the commit baked into the image.

## Adding a language

1. `make i18n-init LOCALE=de` starts `locales/de/LC_MESSAGES/messages.po`.
2. Add `"de"` to `SUPPORTED_LOCALES` in `src/core/i18n/__init__.py`.
3. Add the language's own name to `LANGUAGE_NAMES` in `src/user/keyboards.py`,
   for example `"de": "Deutsch"`.
4. Translate the catalog and run `make i18n-compile`.

`make test` then checks the new catalog: the catalog tests report messages
missing from it or left over, fuzzy entries and changed placeholders, and a
keyboard test fails while a supported language has no name.
