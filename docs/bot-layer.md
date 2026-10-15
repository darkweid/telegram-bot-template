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

On SIGTERM or SIGINT the bot stops taking updates, gives the ones in progress
`SHUTDOWN_TIMEOUT_SECONDS` (20) to finish and cancels the rest, then closes the
HTTP clients and the container. Keep the timeout below the compose
`stop_grace_period` of the bot service (30 seconds), or Docker kills the process
before it closes its connections.

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
- Developers get `/status` and `/test_alert` (`src/developer/handlers.py`).

Neither role is throttled, and a ban never drops their updates. Broadcasts
still skip a banned role holder, and `/stats` counts them as banned until
`/unban`.

The public menu is set for all private chats, and each listed id gets a menu of
its own in its private chat: the public commands plus those of its roles
(`src/main/commands.py`), in every supported language, on every start. An id
that never opened the bot cannot get a menu yet: the bot logs a warning and
starts anyway. Any other refusal from Telegram fails startup.

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
holds per process.

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
through a `Bot` of their own on a session of its own. The same failure (error
class, origin and the innermost project frame) is alerted once per
`ALERT_COOLDOWN_SECONDS` (300), and a task id once in 24 hours however often it
is redelivered. An alert carries ids, the origin, the error class and where it
was raised; the error message only for a `CoreException` or a scrubbed driver or
validation error, since any other message can quote what the user sent. A failed
delivery is logged and never raised into the code being reported on.
`/test_alert` sends a test through the same path, past the cooldown.

## Health

The bot serves three probes on `WEB_SERVER_PORT` (8080) in both modes
(`src/main/health.py`):

- `/live` answers 200 while the process runs; the Docker healthcheck reads it.
- `/ready` answers 200 when PostgreSQL and the three Redis clients answer, and
  503 otherwise.
- `/health` always answers 200 with a JSON report from `HealthService`
  (`src/system/services.py`): `status`, `version`, `commit`, `mode`, the
  PostgreSQL and Redis readings, the Redis memory ratio, the outbox backlog
  and its oldest pending row, the task backlog and the scheduler heartbeat age.
  A reading that failed or timed out (2 seconds) is `null`.

`status` is `degraded` when PostgreSQL or Redis is down, when Redis uses 90% of
`maxmemory`, when the scheduler heartbeat is missing or older than
`HEARTBEAT_TTL_SECONDS` (90), or when an outbox row has waited more than 5
minutes. The task backlog (the workers' consumer group lag plus the pending
entries) is reported but never changes the status. Developers see the same
report with `/status`.
