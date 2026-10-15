# Removing modules

The template carries more than one bot needs. The note module is a reference
domain to copy, and broadcasts are an admin feature a bot may not want. This
document is for the developer who decides to drop one of them: a module goes
out whole, with its migration, its tests and its strings in the catalogs, and
nothing that names it is left behind. Removing a module is the reverse of
[adding one](architecture.md#adding-a-module): the same registration points,
undone.

## Before you start

A module's table came from a migration. What to do with that migration depends
on whether the bot's database lives anywhere but your machine.

- **The bot was never deployed.** The only database is the local one, and it
  can be thrown away. Delete the module's migration files and point the next
  migration's `down_revision` (and the `Revises:` line of its docstring) at the
  parent of the deleted one. Then recreate the local database: `make clean`
  removes the stack with its volumes, and `make run-dev` starts it again and
  applies the shortened chain from scratch.
- **The bot runs somewhere.** A database there has applied the module's
  migrations, and an applied migration is never edited or deleted. Keep the
  files, remove the module from the code, and drop its table with a new
  migration: `make migration m="drop notes"`. Autogenerate sees that the model
  is gone and writes the drop; read the diff before committing.

If you are unsure, take the second path: it works for a fresh database too.

Whichever path you take, `make lint` runs the documentation check, which
refuses a path in backticks that no longer exists. Every file under `docs/`
and `README.md` that names the module has to be rewritten or cut, this
document included: once a module is gone, delete its section here.

## Removing the note module

`src/note` holds the `/note`, `/notes` and `/summarize` commands, the `notes`
table and the `note.note_created` task. It is also the only caller of the LLM
layer; `src/core/llm` stays and has no caller until your own domain adds one.

1. Delete `src/note`, `tests/unit/src/note` and `tests/integration/src/note`.
2. `.importlinter`: remove `src.note` from `core-is-independent`, and
   `src.note.handlers` and `src.note.repositories` from
   `handlers-skip-repositories`.
3. `models/__init__.py`: remove the `Note` import.
4. `src/main/commands.py`: remove the `NOTE_COMMANDS` import, so
   `PUBLIC_COMMANDS` becomes `(*USER_COMMANDS,)`.
5. `src/main/di.py`: remove `NoteProvider`, its import and its line in
   `build_container`.
6. `src/main/dispatcher.py`: remove `build_note_router`, its import and the
   "(note)" mention in the `create_dispatcher` docstring.
7. `src/main/event_subscribers.py`: remove the `NoteCreated` entry and both
   imports.
8. `src/main/tasks.py`: remove `NOTE_TASKS` and its import.
9. `src/core/llm/types.py`: the `LLMRequest` docstring names
   `NOTE_SUMMARY_PURPOSE` as an example purpose; drop the example.
10. Tests outside the package:
    - `tests/unit/src/main/test_tasks.py`: remove
      `test_the_note_subscriber_is_installed`,
      `test_the_note_task_is_registered` and their imports;
    - `tests/unit/src/main/test_dispatcher.py`: remove
      `test_the_note_router_comes_before_the_unmatched_one`;
    - `tests/unit/src/main/test_commands.py`: remove the three note commands
      from the expected Russian menu, and "the note commands" from the
      docstring of `test_each_role_member_gets_the_public_menu_plus_their_role`;
    - `tests/unit/src/user/test_handlers.py`: remove `/note`, `/notes` and
      `/summarize` from `test_help_lists_every_command`;
    - `tests/integration/src/core/database/test_migrations.py`: remove
      `"notes"` from `EXPECTED_TABLES`.
11. The migration. `949f366fa5b8_create_notes` is not the head:
    `a4f80a147123_create_llm_usage` follows it.
    - Never deployed: delete
      `migrations/versions/2026_10_15_1209-949f366fa5b8_create_notes.py` and
      set `down_revision` of `create_llm_usage` to `"5b8e2d41c9a7"`.
    - Deployed: keep both files and run `make migration m="drop notes"`. The
      revision it writes drops the index `ix_notes_user_id_id` and the table
      `notes`, and nothing else.
12. `make i18n-update`: the note strings leave both catalogs (obsolete entries
    are not kept), then `make i18n-compile`.
13. The documentation. The template's `README.md` names `src/note` as the
    reference domain to copy, in the features, the layout and adding a
    module. In `docs/`: `architecture.md` (examples
    throughout, and the reference domain in `## Adding a module`),
    `background-jobs.md` (the `NOTE_TASKS` snippet under
    `## Declaring a task`), `bot-layer.md` (the `CallbackData` example, and
    "note" in the router order), `i18n.md` (the command description example)
    and `llm.md` (the worked example). `RESPONSE_LANGUAGES` lives in
    `src/core/locales.py` and stays. Point each example at
    a module you keep, or cut it.
14. Delete the note section of this document, then run
    `git grep -n -i -w -E 'notes?' -- README.md docs` and rewrite or cut every
    hit that describes the module. The documentation check catches a stale
    path, not a stale sentence.

Some matches of "note" stay and are correct: the test models `Note` in
`tests/unit/src/core/database/`, the `note_summary` purpose strings in the LLM
and `/stats` tests (fixture data, not an import), and `add_note` on exceptions
in the S3 adapter.

Then run `make lint`, `make test` and `make test-integration`.

## Removing broadcasts

`src/broadcast` holds `/broadcast`, its draft dialog, the `broadcasts` table,
and the tasks `broadcast.run` and `broadcast.resume_stalled`. On a deployed bot,
stop any broadcast in progress before the deploy: the deploy runs the
migrations before it replaces the worker.

1. Delete `src/broadcast`, `tests/unit/src/broadcast`,
   `tests/integration/src/broadcast` and `docs/broadcast.md`.
2. `.importlinter`: remove `src.broadcast` from `core-is-independent`, and
   `src.broadcast.handlers` and `src.broadcast.repositories` from
   `handlers-skip-repositories`.
3. `models/__init__.py`: remove the `Broadcast` import.
4. `src/admin/commands.py`: remove the `/broadcast` line from
   `ADMIN_COMMANDS`.
5. `src/main/config.py`: remove `BroadcastConfig`, the `broadcast` field of
   `Config` and its line in `get_settings()`.
6. `src/main/di.py`: remove `BroadcastProvider` and its import, and the
   `broadcast` provide method of `ConfigProvider` with the `BroadcastConfig`
   import.
7. `src/main/dispatcher.py`: remove `build_broadcast_router` and its import.
8. `src/main/tasks.py`: remove `BROADCAST_TASKS` and its import.
9. Settings: remove the `BROADCAST_*` block from `.env.example`, the two keys
   under `# BroadcastConfig` in `KNOWN_KEYS` (`scripts/ops/check_env.py`), and
   the `## Broadcast` section of `docs/configuration.md`. Delete the keys from
   every `.env` as well: `make check-env` warns about a key the template no
   longer reads. Remove `/broadcast` from the `ADMIN_IDS` comment of
   `.env.example` and from its row in `docs/configuration.md`.
10. Tests outside the package:
    - `tests/unit/src/main/test_config.py`: remove
      `test_broadcast_defaults_stay_under_the_bot_api_limit`,
      `test_broadcast_settings_out_of_range_are_refused` and the
      `BroadcastConfig` import;
    - `tests/unit/src/main/test_di.py`: remove
      `test_every_broadcast_run_gets_its_own_pacer` and the
      `RunBroadcastUseCase` and `Pacer` imports;
    - `tests/unit/src/main/test_dispatcher.py`: remove the `broadcast` line
      from `test_role_routers_come_before_the_user_router`;
    - `tests/unit/src/main/test_tasks.py`: remove
      `test_broadcasts_run_once_and_stalled_ones_resume_every_minute` and its
      import.
11. The migrations: `e32a5c7195f8_add_broadcasts` and
    `5b8e2d41c9a7_add_broadcast_reported_at`, followed by
    `949f366fa5b8_create_notes`.
    - Never deployed: delete
      `migrations/versions/2026_10_14_2340-e32a5c7195f8_add_broadcasts.py`
      and `migrations/versions/2026_10_15_1130-5b8e2d41c9a7_add_broadcast_reported_at.py`,
      and set `down_revision` of `create_notes` to `"722c7847da4b"`. With the
      note module gone too, `create_llm_usage` gets that parent instead.
    - Deployed: keep the files and run `make migration m="drop broadcasts"`.
      The revision drops the table `broadcasts` and the enum type
      `broadcaststatus`.
12. `make i18n-update`, then `make i18n-compile`.
13. The documentation. The template's `README.md` lists broadcasts among the
    features and the admin role's powers, in the layout, and links to
    `broadcast.md` from the features and the documentation table. In `docs/`:
    - the links to `broadcast.md` in `background-jobs.md` and `bot-layer.md`
      break with the file;
    - `background-jobs.md` names `RunBroadcastUseCase` as the task that wraps
      its rendering in `i18n.use_locale`: cut that half of the sentence and
      keep the `NotifyAdminsOfNewUserUseCase` half;
    - `i18n.md` names the titles in `src/broadcast/texts.py` as an `N_`
      example, and ends its section on text outside an update with the
      `i18n.use_locale` pattern of the broadcast report: cut that sentence,
      since no other task renders that way;
    - `bot-layer.md` has "broadcast" in the router order, and names
      broadcasts under reachability and roles;
    - `architecture.md` lists the package and uses `StartBroadcastUseCase` as
      the advisory-lock example.
14. Delete the broadcast section of this document, then run
    `git grep -n -i broadcast -- README.md docs` and rewrite or cut every hit
    that describes the module. The documentation check catches a stale path,
    not a stale sentence.

What stays:

- **The index `ix_users_recipients`** on `users` and the repository methods
  `get_recipient_ids` and `count_recipients` in `src/user/repositories.py`.
  They answer "who can the bot write to": reachable, unbanned users in id
  order, which any mass message needs. The index came from
  `722c7847da4b_add_user_bans`, not from a broadcast migration, so neither
  path touches it, and
  `test_broadcast_recipients_have_a_partial_index_on_id` keeps passing. A bot
  that will never message its users in bulk can drop all of it: remove the
  `Index("ix_users_recipients", ...)` from `__table_args__` in
  `src/user/models.py` and run `make migration m="drop recipients index"`
  (autogenerate writes the drop only once the model no longer declares the
  index), then remove `_recipient_filter` and the two methods from
  `src/user/repositories.py`, their tests in
  `tests/integration/src/user/test_user_repository.py`, and
  `test_broadcast_recipients_have_a_partial_index_on_id` from
  `tests/integration/src/core/database/test_migrations.py`.
- **`Pacer`** in `src/core/limiter/pacer.py`, a local pace for any loop of Bot
  API calls; nothing provides it once `BroadcastProvider` is gone.
- Comments that give broadcasts as the reason for a rule: in
  `src/user/models.py`, `src/user/repositories.py`, `src/user/middlewares.py`,
  `src/core/limiter/pacer.py`, `src/core/telegram/session.py` and
  `infra/docker-compose.yml`. The rules hold without broadcasts; reword the
  reason when you touch the file.

Then run `make lint`, `make test` and `make test-integration`.

## Removing another domain module

Every domain registers itself in the same places, so the two recipes above
generalize. Search the tree for the package name (`git grep -n -i <module>`)
and work through:

- the package, `tests/unit/src/<module>` and `tests/integration/src/<module>`;
- both contracts in `.importlinter`;
- the model import in `models/__init__.py`;
- the provider in `build_container` (`src/main/di.py`), and a config section's
  provide method in `ConfigProvider` if the module has settings;
- the router in `create_dispatcher` (`src/main/dispatcher.py`);
- its commands in `PUBLIC_COMMANDS` (`src/main/commands.py`) or a role's
  command tuple;
- its events in `EVENT_SUBSCRIBERS` (`src/main/event_subscribers.py`) and its
  tasks in `TASKS` (`src/main/tasks.py`);
- its settings: the section in `src/main/config.py`, the keys in
  `.env.example`, `KNOWN_KEYS` and `docs/configuration.md`;
- tests under `tests/unit/src/main/` that assert on the registration points
  above, and `EXPECTED_TABLES` in the migration test;
- the migration, by one of the two paths in
  [Before you start](#before-you-start);
- the catalogs (`make i18n-update`, `make i18n-compile`) and the documentation.

`src/user` is not a removable module: the middleware chain, roles, bans and
reachability all depend on it.

## Features that are off by default

Three features ship switched off. A switched-off feature needs no removal: the
container provides a stand-in, so it builds with `STRICT_VALIDATION` and the
bot starts without any of the feature's keys. Only a call into the feature
fails, with an error that says what is missing.

| Feature | Off when | Stand-in | On use |
|---|---|---|---|
| LLM | `LLM_PROVIDER` is empty | `DisabledLLMClient` (`src/core/llm/disabled.py`) | raises `LLMNotConfigured`; the user is told the feature is not available |
| S3 | `S3_ENABLED=false` | `DisabledS3Client` (`src/core/storage/s3/disabled.py`) | raises `S3Disabled`, naming the keys to set |
| Email | `EMAIL_ENABLED=false` | `DisabledMailer` (`src/core/email/disabled.py`) | raises `EmailDisabled`, naming the keys to set; `EmailService` refuses to queue before any outbox row is written |

Their packages stay in the image while they are off: `anthropic`, `openai`,
`aioboto3`, `aiosmtplib` and `jinja2` are listed in
`infra/requirements/base.in` and installed whatever the switches say. These
features are not domain modules and the recipes above do not cover them: the
LLM layer, for one, also owns the `llm_usage` table, its purge task and the
spending block of `/stats`. Leaving a feature switched off costs image size,
not behavior.
