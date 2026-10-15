# The bot layer

What the bot process does with an update from the moment Telegram delivers it:
the two modes, startup and shutdown, the middleware chain, roles, throttling,
the error handler and developer alerts, and the health probes. Open it when
adding a handler, a router or a middleware, or when an update is dropped,
throttled or answered in a way you did not expect. How handlers reach use cases
is in [the architecture](architecture.md#layers).

## Modes

`BOT_MODE` picks how updates arrive; both modes run the same dispatcher, built
by `create_dispatcher` in `src/main/dispatcher.py`.

- **`polling`** (the default; the dev stack always polls). The bot deletes any
  webhook first, keeping pending updates, then long-polls with the update
  types its routers use (`src/main/polling.py`). Run one polling process per
  token: Telegram answers a second one with a conflict.
- **`webhook`**. The bot serves the Telegram route on `WEBHOOK_PATH` next to the
  health probes (`src/main/webhook.py`) and calls `setWebhook` on every start,
  once the server listens; it never deletes the webhook on shutdown. The
  configuration refuses this mode without an `https` `WEBHOOK_BASE_URL` and a
  `WEBHOOK_SECRET` of 32 to 256 characters (`A-Z`, `a-z`, `0-9`, `_`, `-`).

Startup is shared (`bot_runtime` in `src/main/runtime.py`): the bot builds the
container, checks PostgreSQL and every Redis client (cache, FSM state, task
queue), builds the dispatcher and sets the command menus. Every failure there
is a `StartupError`: `python -m src.main` logs it as one CRITICAL line without a
traceback and exits with status 1. An unreachable database and a refused
`setWebhook` both end this way; the webhook message names the error class and
Telegram's description, never the secret.

On SIGTERM or SIGINT the bot stops taking updates and gives the ones in
progress `SHUTDOWN_TIMEOUT_SECONDS` (20) to finish. The background work they
started (see [Background work](#background-work)) gets what is left of the
same timeout; whatever still runs then is cancelled, and the bot waits for its
cleanup: a cancelled model call is recorded and its status reads
"Interrupted", each within 2 seconds. Then the bot closes the HTTP clients and
the container. Docker waits `BOT_STOP_GRACE_PERIOD` (30s) before it kills the
process; `make check-env` refuses a grace period shorter than the timeout
plus 5 seconds.

## Middleware chain

The order matters; it is set in `create_dispatcher`. Outer middlewares on
`dp.update` run after aiogram's own (errors, user context, FSM context):

1. `UpdateContextMiddleware` binds the update id, its type and the user id for
   every log line and Sentry event, and opens the update's flood-wait budget.
2. dishka's `ContainerMiddleware` opens the update's REQUEST scope, which the
   middlewares below and the handler share.
3. `UserMiddleware` (`src/user/middlewares.py`) stores the sender of a private
   chat and puts the stored user into `data["user"]`. It reads the user's
   snapshot from the cache and goes to the database only when the snapshot is
   missing or `needs_write` (`src/user/services.py`) says the row is due: the
   profile changed, the user was marked unreachable, or the row was last
   written more than `USER_TOUCH_INTERVAL_SECONDS` ago. It skips bots, groups,
   channels and `my_chat_member` updates.
4. `UserLocaleMiddleware` picks the reply language (see
   [marking strings](i18n.md#marking-strings)).
5. `BanMiddleware` drops a banned user's updates without an answer, unless
   the user holds a role. Only updates that carry a stored user are checked,
   so a router for groups or inline queries checks `is_banned` itself.

Inner middlewares run only around a handler that matched:

- `SkipAwareCallbackAnswerMiddleware` on `dp.callback_query` answers every
  callback query, so the button's spinner stops even when the handler fails.
  A handler can still set `callback_answer.text`.
- `ThrottlingMiddleware` on `dp.message` and `dp.callback_query`, after the
  callback answer, so a throttled callback still gets its one answer.

Routers are built by factories (`build_user_router()`, `build_note_router()`
and so on), never at module level: aiogram attaches a router to one parent
only, and every test builds a dispatcher of its own. They are included in this
order: errors, admin, developer, broadcast, user, note, then `unmatched`. Role
routers come first so that an admin command reaches the admin handler even when
a user handler would match it too. The `unmatched` router is always last and
takes every callback no other router did (a stale button, a group chat), so the
callback answer middleware still answers it.

Two conventions for handlers. A handler takes the event as its first parameter
even when it does not use it, since aiogram passes it positionally. A message is
edited through `safe_edit_text` (`src/core/telegram/messages.py`), which treats
"message is not modified" after a double tap as success. Callback data is one
`CallbackData` class per purpose (`src/note/callbacks.py`). Replies use HTML
parse mode, so a user-controlled value in a reply goes through
`aiogram.html.quote`.

Every Bot API call also passes through two session middlewares
(`src/core/telegram/session.py`):

- `RetryAfterMiddleware` retries Telegram's flood control up to
  `TELEGRAM_RETRY_ATTEMPTS` (3) times. All calls of one update share 30 seconds
  of sleep, half of the 60-second lock that keeps one user's updates in order;
  a wait past the budget fails the call.
- `ForbiddenMiddleware` marks the user unreachable when Telegram refuses a
  private chat with Forbidden (a blocked bot, a deleted account), then re-raises.

`users.is_reachable` turns false on that Forbidden and on a `my_chat_member`
update saying the bot was blocked (`kicked`), and true again on the user's
next update or a `my_chat_member` update saying it was unblocked (`member`).
Broadcasts go to reachable users only. A ban is a separate state set by an
admin and does not touch reachability.

## Roles

`ADMIN_IDS` and `DEVELOPER_IDS` are comma-separated Telegram user ids. They
become `Roles` (`src/core/telegram/roles.py`), which the dispatcher passes to
every handler and filter; `IsAdmin()` and `IsDeveloper()` guard the role
routers, which also accept private chats only.

- Admins get `/ban`, `/unban`, `/stats` (`src/admin/handlers.py`) and
  `/broadcast` ([broadcasts](broadcast.md#sending-a-broadcast)).
- Developers get `/status`, which shows the [health report](#health), and
  `/test_alert` (`src/developer/handlers.py`).

`/ban` and `/unban` take a Telegram user id or an `@username`, matched
ignoring case. Only a user who has started the bot can be named, and a
username that several stored users have had is refused: use the id. Admins
and developers cannot be banned, and nobody bans themselves. A banned user's
updates are dropped without an answer.

A ban outlives a Redis outage. While Redis is down every update reads the
stored row, so a ban or unban made then holds at once. The user snapshot
cached before it is never served again: the bot remembers that its delete
failed and retries it on the next read, and the change also queues the
`user.drop_snapshot` task, which deletes the snapshot once Redis is back.
Only a bot restarted during the outage forgets the failed delete, and then
serves the old snapshot until the task lands, usually within a couple of
minutes of Redis coming back.

`/stats` shows users in total, reachable, banned, and active and new in the
last 1, 7 and 30 days, then the LLM spending: today since midnight UTC, the
last 7 and 30 days, the last 30 days by purpose and for the top 5 users, and
the calls that failed or had no known price when there are any.

Neither role is throttled, and a ban never drops their updates. A role beats
a ban for updates only: someone banned before their id was added to
`ADMIN_IDS` or `DEVELOPER_IDS` is served as usual, but broadcasts skip them and
`/stats` counts them as banned until `/unban`.

The public menu is set for all private chats, and each listed id gets a menu of
its own in its private chat: the public commands plus those of its roles
(`src/main/commands.py`), in every supported language, on every start. An id
that never opened the bot cannot get a menu yet: the bot logs a warning and
starts anyway, and the menu is set on the first start after that user opens
the bot. Any other refusal from Telegram fails startup.

The bot never deletes a menu it set. Removing an id from `ADMIN_IDS` or
`DEVELOPER_IDS` takes the role away at once, but Telegram keeps showing that
user the old menu; clear it once by hand with `deleteMyCommands` for the scope
`{"type": "chat", "chat_id": <id>}` and every language code the bot set.

## Throttling

The limit counts handler calls per user. A handler declares its profile with a
flag; without one it gets `Throttle.DEFAULT`:

```python
router.message.register(summarize, Command("summarize"), flags={"throttle": Throttle.LLM})
```

| Profile | Limit | Window |
| --- | --- | --- |
| `Throttle.DEFAULT` | `THROTTLE_DEFAULT_LIMIT` (20) | `THROTTLE_DEFAULT_WINDOW_SECONDS` (60) |
| `Throttle.LLM` | `THROTTLE_LLM_LIMIT` (5) | `THROTTLE_LLM_WINDOW_SECONDS` (60) |

The middleware is inner because the flag is known only once a handler is
chosen, so an update no handler takes is never counted. Over the limit, the
first refused update of the window gets one warning (a callback gets it as its
answer) and the rest are dropped. A handler that raises `SkipHandler` passes
the update to the next matching one, and aiogram runs the inner middlewares
again, so such an update counts once per handler reached. Counters live in
Redis; while Redis is down each process counts in its own memory, so the limit
holds per process. The keys and their defaults are in
[the configuration reference](configuration.md#throttling).

## Errors and alerts

One error handler (`src/core/telegram/errors.py`) treats every handler
failure:

- `TelegramForbiddenError` is logged at info and nothing is sent: there is
  nobody to tell.
- A `CoreException` subclass is an expected refusal. It is logged at its
  `log_level`, sent to Sentry when `capture_to_sentry` is set, and the user is
  told its `message_key`, filled with its `params`.
- Anything else is logged as an error, sent to Sentry, answered with a generic
  message and alerted to the developers.

The reply goes out in the sender's Telegram language: the error handler runs
from aiogram's errors middleware, outside every middleware of ours, so the
stored user and its chosen language are gone.

A domain error is a subclass, not a new handler:

```python
class NoteNotFoundError(CoreException):
    message_key = N_("This note no longer exists.")
    log_level = "info"
```

`message_key` is the English text marked with `N_`. `CoreException(detail,
**params)`: `detail` goes to the log line only, `params` fill the message's
placeholders through `format_html`, which escapes them. `InfrastructureException`
is the base for a dependency that failed: it logs at error and goes to Sentry.

Developer alerts (`DeveloperAlerter`, `src/core/telegram/alerts.py`) are sent
when a handler raises an unexpected error, when a task fails for good, when the
LLM client switches to its fallback provider, and when Redis goes down under the
cache or the throttle limiter, and again when it recovers. They go to
`DEV_ALERT_CHAT_ID` when it is set, otherwise to every developer in private,
through a `Bot` of their own on a session of its own. The bot must be a member
of the alert chat, and a developer must have opened the bot: an alert Telegram
refuses is logged and nothing more. The keys are in
[the configuration reference](configuration.md#bot).

An alert carries the error class, where it was raised, its origin (what was
being handled: `update message`, `task <name>`, or `llm <provider>` for a
fallback switch, naming the provider that failed), the user id or task id, and
the Sentry event id when Sentry is on. The error message (cut to 500
characters) is shown only for a `CoreException` or a scrubbed driver or
validation error, whose text is rebuilt from safe fields; any other message can
quote what the user sent (`int(message.text)`), so the alert points to the logs
instead. The same failure (error class, origin and the innermost project frame)
is alerted once per `ALERT_COOLDOWN_SECONDS` (300), a fallback switch included,
and a task id once in 24 hours however often it is redelivered. A failed
delivery is logged and never raised into the code being reported on.
`/test_alert` sends a test through the same path, past the cooldown.

## Background work

A handler that would hold its update for seconds (a model call, an agent run)
starts background work instead and returns. The update ends at once, so the
user's next updates do not wait behind its event-isolation lock, and the work
answers by editing a status message. `/summarize` (`src/note/handlers.py`) is
the example:

```python
status = await ProcessingStatus.send(
    bot, message.chat.id, i18n=i18n, text=_("Summarizing your notes…")
)
background.start(
    "note.summarize",
    partial(
        summarize_in_background,
        status=status,
        user_id=user.id,
        locale=i18n.current_locale,
    ),
)
```

`BackgroundRunner` (`src/core/telegram/background.py`, APP scope, a handler
takes it as `FromDishka[BackgroundRunner]`) runs each piece of work in a task
of its own:

- The work receives the root container and opens `async with container() as
  scope` for each transaction or service it needs: the update's scope closes
  with the update, long before the work ends.
- The task copies the update's context: the update's ids stamp every log
  line, and `_()` translates in the user's locale. It gets a flood-control
  sleep budget of its own, since the update may have spent its budget.
- The runner reports what the work raises the way the error handler would: a
  `CoreException` at its own level (`report_core_exception`,
  `src/core/errors/reporting.py`), a `TelegramForbiddenError` at info,
  anything else as an error, to Sentry and to the developers with origin
  `background <name>`. It never writes to the user: the status does.

`ProcessingStatus` (`src/core/telegram/status.py`) is the message the user
watches. The handler sends it ("⏳ Working on it…" or its own text) in the
user's locale. The work enters `async with status:`, calls
`status.update(text)` as it goes and ends with `await status.finish(answer)`.
Inside the block the status keeps the typing indicator on and shows the
latest update at most once a second; `update` never waits for Telegram, and
a text overtaken before it was shown is dropped. `finish` waits out flood
control and sends the answer as a new message when the user deleted the
status. Leaving the block with an exception edits the status into the text
the error handler would have sent; a cancel edits it into "Interrupted.
Please try again."; leaving without `finish` is a bug and raises
`RuntimeError`. Texts are HTML: put user values through `html.quote`.

Calls whose result is about to be replaced anyway (the typing action, a
progress edit) run under `without_flood_retry()`
(`src/core/telegram/session.py`): `RetryAfterMiddleware` raises the flood
wait at once instead of sleeping it out, and the status stays quiet for that
long. The answer is retried as usual.

## Health

The bot serves three probes on `WEB_SERVER_PORT` (8080) in both modes
(`src/main/health.py`). No port of the bot is published: the probes are
reached over the compose network, and in webhook mode nginx passes them on
only from private addresses
([nginx and the certificate](deployment.md#nginx-and-the-certificate)).

- `/live` answers 200 while the process runs; the Docker healthcheck reads it.
- `/ready` answers 200 when PostgreSQL and the three Redis clients answer, and
  503 otherwise.
- `/health` always answers 200 with a JSON report from `HealthService`
  (`src/system/services.py`): `status`, `version`, `commit`, `mode`,
  `postgres`, `redis`, `redis_memory_used_ratio`, `outbox_pending`,
  `outbox_oldest_pending_seconds`, `task_queue_length` and
  `scheduler_heartbeat_age_seconds`. A reading that failed or timed out
  (2 seconds) is `null`, and so is every Redis reading while any of the three
  Redis databases is down.

`status` is `degraded` when PostgreSQL or Redis is down, when Redis uses 90% of
`maxmemory`, when the scheduler heartbeat is missing or older than
`HEARTBEAT_TTL_SECONDS` (90), or when an outbox row has waited more than 5
minutes. The task backlog, `task_queue_length`, is the workers' consumer group
lag plus the entries delivered and not yet acknowledged, not the stream's
length, which keeps acknowledged tasks until the stream is trimmed. It is
`null` when Redis cannot count the undelivered ones, which happens after some
trims and deletions, and it never changes the status: there is no backlog that
is wrong for every bot. Developers see the same report with `/status`.
