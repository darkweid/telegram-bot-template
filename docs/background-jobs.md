# Background jobs

How work leaves a handler and runs later: the worker and the scheduler, how a
task is declared and enqueued, what the outbox guarantees, the scheduled jobs,
failure alerts, and the two integrations that run through tasks, S3 file
storage and email. Open it when adding a task or a schedule, when a task did
not run or ran twice, or before turning S3 or email on. How a use case writes
the outbox row inside its transaction is in
[the architecture](architecture.md#domain-events-and-the-outbox).

## Processes

Background work runs on [taskiq](https://taskiq-python.github.io/) over a Redis
stream. The queue, the delayed retries and the scheduler heartbeat live in their
own Redis database, `REDIS_TASKS_DATABASE` (`1`), apart from the cache and the
FSM state. Three services run the same image:

- `bot` enqueues tasks. Its broker starts on the first send, so a tasks-Redis
  outage fails no update that enqueues nothing.
- `worker` runs them (`src/main/worker.py`): one process, up to 20 tasks at
  once; scale it with replicas. It builds the same container as the bot, and
  taskiq's dishka integration gives each task a REQUEST scope, so a task
  receives use cases through `FromDishka[...]` like a handler. On stop it waits
  up to 25 seconds for running tasks (`--wait-tasks-timeout`), under its
  `stop_grace_period` of 35 seconds.
- `scheduler` fires cron schedules and the worker's delayed retries
  (`src/main/scheduler.py`). Run **exactly one**: taskiq has no duplicate-fire
  protection, so two schedulers fire every cron twice. Its loop writes a
  heartbeat every 30 seconds into a key that lives 90 seconds; the compose
  healthcheck (`python -m src.main.heartbeat`) and `/health` read it.

`make logs s=worker` and `make logs s=scheduler` show their output. In the dev
stack the worker restarts on a code change like the bot; the scheduler does not,
so run `make restart s=scheduler` after changing a schedule.

## Declaring a task

A task is a `TaskSpec` (`src/core/tasks/registry.py`) declared in its domain
and listed in `TASKS` (`src/main/tasks.py`):

```python
NOTE_CREATED = "note.note_created"


async def note_created(note_id: str, user_id: int) -> None:
    logger.info("Note %s created by user %s", note_id, user_id)


NOTE_TASKS: tuple[TaskSpec, ...] = (TaskSpec(NOTE_CREATED, note_created),)
```

A task missing from `TASKS` is invisible to the worker, and its outbox rows
are marked failed. The fields:

- `name`: what the outbox stores and the worker looks up.
- `schedule`: taskiq's cron label, for example `({"cron": "* * * * *"},)`.
- `retry_on_error=True`: on an error the task runs again a minute later (with
  jitter), three attempts in all. The default is no retry, which suits
  anything a person would see twice. A delayed retry is fired by the scheduler.
- `at_most_once=True`: the worker records the task as done before running it,
  so a delivery reclaimed from a worker that stopped midway is dropped instead
  of run again. It is for a message a person reads, where a lost one beats a
  repeated one (`user.notify_admins_of_new_user`). It cannot be combined with a
  retry.

Without `at_most_once` a task runs at least once: a worker that stops midway
leaves the delivery to another worker, which reclaims it after ten minutes.
Every task is wrapped so its error leaves it already scrubbed of user input,
before taskiq logs it.

A task runs outside any update, so no locale is active. A task that sends text
translates it in the recipient's language: `RunBroadcastUseCase`
(`src/broadcast/usecases/run_broadcast.py`) wraps its rendering in
`with self._i18n.context(), self._i18n.use_locale(locale)`, and
`NotifyAdminsOfNewUserUseCase` (`src/user/usecases/notify_admins.py`) calls
`i18n.gettext(msgid, locale=...)` per recipient (see
[marking strings](i18n.md#marking-strings)).

Release order matters when a new task is added. During a deploy the new bot
already serves while the old worker still runs, and a task the old worker does
not know is lost. So a release registers a task before the release that
enqueues it.

## The outbox

A use case enqueues only through its unit of work, never with taskiq's `kiq()`:

```python
async with self._uow as uow:
    ...
    await uow.enqueue_task("reports.send", user_id=user.id)
    await uow.commit()
```

- The call stores a row in `outbox_messages` in the same transaction, so the
  task exists only if the work that caused it commits.
- Arguments are ids and plain JSON values that survive JSONB unchanged
  (`src/core/outbox/payload.py`); anything a round trip would change is
  refused. Tasks carry ids, never what a user typed; the email tasks are the
  exception.
- After the commit `OutboxPublisher` (`src/core/outbox/publisher.py`) sends the
  row with its id as the task id. A send waits at most 5 seconds; a failed one
  leaves the row pending.
- `outbox.sweep` runs every minute and sends rows still pending after a
  60-second grace, so a lost send reaches the worker within a minute or two. An
  unreachable or full tasks Redis does not count as an attempt: the row waits
  for Redis. Any other send error counts, and a row fails for good after 10
  attempts; a task name missing from `TASKS` fails it at once.
- The worker's `IdempotencyReceiver` (`src/core/tasks/receiver.py`) keeps a
  completion marker per task id for 7 days and a running claim while a task
  runs, so a row sent twice runs once.
- `outbox.purge` deletes published rows older than `PUBLISHED_RETENTION_DAYS`
  (7) in batches, each committed on its own. A row still pending after those 7
  days is never sent: the sweeper marks it failed and logs it at CRITICAL,
  since the worker remembers a finished task only as long and a late send could
  run it twice.

A domain event is the usual way a task starts: the subscriber enqueues it in
the event's transaction (`src/main/event_subscribers.py`).

## Schedules

| Task | Cron (UTC) | What it does |
| --- | --- | --- |
| `outbox.sweep` | `* * * * *` | sends outbox rows the publisher could not |
| `outbox.purge` | `0 3 * * *` | deletes published outbox rows older than 7 days |
| `llm_usage.purge` | `30 3 * * *` | deletes `llm_usage` rows older than `LLM_USAGE_RETENTION_DAYS` (90) |
| `broadcast.resume_stalled` | `* * * * *` | resumes broadcasts whose run stopped, and sends reports that never went out ([how a run works](broadcast.md#how-a-run-works)) |

A new schedule is a `schedule=` on the task's `TaskSpec`; restart the scheduler
to pick it up.

## Failure alerts

`TaskFailureAlertMiddleware` (`src/core/tasks/middlewares.py`) comes first in
the broker's middleware list. taskiq runs error hooks in reverse order, so it
sees a failure after the retry middleware decided, and alerts the developers
only for a task that failed for good. One task id is alerted at most once in
24 hours, so a redelivery stays silent. A task cancelled by a stopping worker
is not a failure and alerts nobody. The alert carries the task name, its id and
the error class; the format and the cooldown are described in
[errors and alerts](bot-layer.md#errors-and-alerts).

## File storage (S3)

`src/core/storage/s3` is an async client for S3 and S3-compatible storage
(MinIO, R2) on aioboto3. It is off by default: with `S3_ENABLED=false` the
container holds a `DisabledS3Client`, which raises `S3Disabled` on every call,
so a bot that stores no files needs no keys. `S3_ENABLED=true` requires
`S3_BUCKET_NAME`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` and
`S3_REGION_NAME`; the configuration refuses to start without them. Every
`S3_*` key is in [the configuration reference](configuration.md#s3).

Use cases and tasks receive the client as `S3Client`
(`src/core/storage/s3/interface.py`):

```python
await s3.upload_stream(f"users/{user_id}/avatar.jpg", file, content_type="image/jpeg")
```

`upload_stream` takes any binary stream, reads it a part at a time and switches
to a multipart upload past one part. A stream larger than
`S3_MAX_UPLOAD_SIZE_BYTES` (20 MiB) fails with `S3ObjectTooLarge` and stores
nothing. The interface also downloads, deletes, lists, copies and signs URLs.
The client opens on first use and closes with the container.

## Email

`src/core/email` sends email over SMTP (aiosmtplib) from Jinja2 templates in
`src/core/email/templates`: `base.html`, its components and `notification.html`
(a title and a message, filled from `NotificationEmail`). It is off by default:
with `EMAIL_ENABLED=false` the mailer raises `EmailDisabled` and `EmailService`
refuses to queue an email. `EMAIL_ENABLED=true` requires `EMAIL_SERVER`,
`EMAIL_USER` and `EMAIL_FROM_NAME`; the port and TLS keys are in
[the configuration reference](configuration.md#email).

A use case queues an email in its own transaction, and the worker sends it
after the commit through the `email.send` task:

```python
async with self._uow as uow:
    ...
    await self._email.enqueue_template_email(
        uow,
        "Your report is ready",
        address,
        NOTIFICATION_TEMPLATE,
        NotificationEmail(title="Report", message=text),
    )
    await uow.commit()
```

Invalid addresses are skipped; when none is valid the call raises
`ValueError`. `enqueue_email_with_s3_attachments` (task
`email.send_with_s3_attachments`) attaches files stored in S3, needs S3 turned
on, and with `cleanup=True` deletes them once the email went out; a failed
deletion is only logged. Both email tasks are retried on failure.

The email tasks are the exception to "tasks carry ids only": the recipients,
subject, body and template context are stored in the outbox row (kept 7 days)
and in the task stream, because the send needs them. Logs carry the template
name and counts only, and an SMTP error is reduced to its class and reply code,
since the server's reply quotes the addresses it refused.
