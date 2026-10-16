# Testing

How the test suite is laid out, how to run each part, and which fakes and
helpers a new test should reach for. Open this document before you write the
first test for a handler, a use case or a repository of your bot.

## Running the tests

| Command | What it runs |
|---|---|
| `make test` | `TESTING=true pytest`: the unit suite, no Docker needed. |
| `make test-cov` | The same with a coverage report (terminal and `coverage.xml`). |
| `make test-integration` | `tests/integration` against a throwaway PostgreSQL and Redis. |
| `make test-all` | The unit suite, then the integration suite. |

One file: `TESTING=true pytest tests/unit/src/<module>/test_<name>.py`.
`TESTING=true` makes the settings come from `.env.test` instead of `.env`, so
a test never depends on the values of your local bot.

`pytest.ini` sets the rules every test runs under:

- `asyncio_mode = auto`: an `async def test_...` needs no marker;
- `addopts = -m "not integration"`: a bare `pytest` skips the integration
  suite. A command line with its own `-m` replaces this default, so spell the
  exclusion out when you combine marker filters;
- `filterwarnings = error`: a warning fails the test that raised it.

`pytest-randomly` (in `infra/requirements/dev.in`) shuffles the order on
every run. A test that fails on some seed leaks state into its neighbors:
fix the leak, never pin the order.

Tests never depend on the calendar or the shell. No absolute dates and no
second call to the wall clock: inject or monkeypatch `get_utc_now`. The
autouse fixture in `tests/unit/conftest.py` hides every config variable
exported in your shell except `TESTING`, so a `BOT_MODE` or `DEBUG` set there
cannot decide whether a unit test passes; a test that needs one sets it
itself.

`tests/unit` mirrors the source tree: the tests of `src/user/handlers.py` live
in `tests/unit/src/user/test_handlers.py`. A test docstring says why the
behavior matters, not what the test does.

## Handler tests

A handler test drives the production dispatcher, its middlewares and its
routers, on a fake network. `running_bot(config, i18n, *providers,
alert_session=...)` from `tests/helpers/dispatcher.py` builds the container,
starts the dispatcher the way polling and webhook start it, and stops it on
exit:

```python
async with running_bot(settings, i18n, UseCases(), stub) as running:
    await running.feed(build_message_update("/note"))
    await running.feed(build_message_update("Buy milk"))
prompt, saved = running.session.sent(SendMessage)
```

- **Updates** come from `tests/factories/updates.py`:
  `build_message_update`, `build_callback_update` and
  `build_my_chat_member_update`, each from user 42 in a private chat unless
  told otherwise.
- **The network** is `FakeTelegramSession` (`tests/fakes/telegram.py`), which
  records every Bot API call. `session.sent(SendMessage)` returns the calls
  of one method; `session.queue(Method, result)` sets the next answer,
  an exception included; `await session.wait_for(Method, count)` waits for a
  call made in the background and fails after 2 seconds. A method that
  returns neither a bool, a message nor a `MessageId` must be queued, so a
  test never passes on an answer it did not ask for.
- **Alerts** leave through a bot of their own. Pass `alert_session=` a second
  `FakeTelegramSession` and assert on it, never on `running.session`.
- **Overrides.** `running_bot` always adds `TestOverrides`
  (`tests/helpers/providers.py`): in-memory FSM storage, cache and limiters,
  and recording fakes for the task publisher (`.publisher`), the unreachable
  recorder (`.recorder`), the model (`.llm`, a `FakeLLMClient`) and the
  transcriber (`.transcriber`, a `FakeTranscriber`). Inside a
  running bot, get them from the container, for example
  `await running.container.get(LLMClient)`. `SessionOnly` overrides only the
  two Telegram sessions and the catalogs, for a test of a production provider
  that `TestOverrides` would replace.
- **Background work.** A handler that starts background work returns before
  the work ends. `await running.settle()` waits for every task the runner
  holds, and the tasks they start, and fails after 2 seconds.
  `FakeLLMClient.hold()` returns a `Hold`: the next call made waits for
  `hold.released` once it set `hold.reached`, so a test can look at the bot
  while the model is thinking.
- **Your providers** come after `i18n`. `UseCases` (`tests/helpers/user.py`)
  replaces the user use cases with mocks and stores a user with a chosen
  locale or ban. The container runs with strict validation: every overriding
  `@provide` carries `override=True`, or the container refuses to build.
- **Logs.** Assert on log lines through `captured_logs(name)` from
  `tests/helpers/logs.py`. pytest's `caplog` sees nothing: the project's
  loggers do not propagate to the root logger.

## Integration tests

`tests/integration` runs against real PostgreSQL and Redis. `make
test-integration` starts them from `infra/docker-compose.test.yml` under a
compose project name of its own with ephemeral host ports, passes the ports
to pytest, and removes the project and its volumes on exit, so it never
touches the dev stack.

- **The marker** comes from the location: `pytest_itemcollected` in
  `tests/integration/conftest.py` marks every test collected there as
  `integration`. A new file needs no `pytestmark` for that.
- **One event loop.** The engine and its connections live on the session
  loop, and an asyncpg connection cannot cross loops. A module whose tests
  use the shared fixtures starts with
  `pytestmark = pytest.mark.asyncio(loop_scope="session")`, and every async
  fixture is `@pytest_asyncio.fixture(loop_scope="session")`.
- **Fixtures.** `db_session` is a session on the migrated test database;
  `redis_client` is a client on the throwaway Redis. The schema comes from
  running the Alembic migrations once per session, so the migrations are
  tested on every run.
- **Scope.** Only the SQL and Redis boundary belongs here: migrations,
  unit-of-work semantics, upserts, row and advisory locks, Lua scripts, FSM
  storage, outbox delivery. If a fake can answer the question, it is a unit
  test.

## Fakes and helpers

| Name | Where | What it stands in for |
|---|---|---|
| `InMemoryRedis` | `tests/fakes/redis.py` | Redis, including every Lua script the code runs (rate limiter, cache, task claim). |
| `FakeLLMClient` | `tests/fakes/llm.py` | `LLMClient`: `queue()` a response or an exception, `queue_structured()` an output model, `hold()` the next call, read `calls`. |
| `FakeTranscriber` | `tests/fakes/speech.py` | `Transcriber`: `queue()` a `transcript()` or an exception, `hold()` the next call, read `calls`; refuses the input the adapter refuses. |
| `ScriptedLLMServer` | `tests/helpers/llm_server.py` | The provider's HTTP API, as an `httpx2` mock transport for the adapter tests (`tests/unit/src/core/llm/providers/test_anthropic.py`, `test_openrouter.py`). |
| `InMemoryS3Client` | `tests/fakes/s3.py` | The `S3Client` protocol over in-memory buckets. |
| `FakeMailer` | `tests/fakes/email.py` | The SMTP mailer; records every send, or raises the `error` you set. |
| `FakeHttpClient` | `tests/fakes/http.py` | `HttpRequester`, the protocol `HttpClient` implements, for an integration's tests (type the dependency as `HttpRequester`); records requests and answers from a queue, repeating the last. |
| `FakeAsyncSession` | `tests/fakes/db.py` | An `AsyncSession` for use cases whose repositories are mocks. |
| `full_redis` | `tests/helpers/redis.py` | Turns the throwaway Redis full under `noeviction`, for out-of-memory paths. |
| `with_llm` | `tests/helpers/llm.py` | `settings` with an LLM section built from keyword values alone. |
| `build_user_model`, `build_user_snapshot` | `tests/factories/users.py` | A stored user row or its cached snapshot. |

Each Lua emulation in `InMemoryRedis` has an integration test that runs the
same case on the real script, under `tests/integration/src/core/limiter`,
`cache` and `tasks`. A new script needs both, or the fake can drift from the
script it stands in for.

The autouse fixture `_restore_event_subscribers` in `tests/conftest.py`
restores the domain-event registry after every test, so a test may subscribe
freely. The same file provides the `settings` and `i18n` fixtures, the latter
on catalogs compiled from the working tree.

## Coverage

`make test-cov` enforces 90% coverage over `src/`, branches included
(`.coveragerc`, `fail_under = 90`); `src/main/__main__.py` is left out.

Uploading the report to Coveralls is off until the repository variable
`COVERALLS_ENABLED` is `true`; a public repository needs no token, the
workflow token is enough. With it on, every push gets a Coveralls build,
including a push whose tests CI skips because the tree already passed or the
change touches documentation only.

A push that skips the unit tests carries over the coverage report of the
nearest commit whose tree was tested: its own tree, or an earlier commit's
when only documentation changed in between. When that lookup finds no report
(none was kept, it expired, or the API call failed), the unit tests run again
on that push, and a failure there fails CI like any other failed test. A
report that was found but cannot be downloaded afterwards leaves the commit
without a Coveralls build and fails nothing.
