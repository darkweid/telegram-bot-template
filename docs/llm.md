# The LLM layer

`src/core/llm` puts a language model behind one interface, `LLMClient`, with
fallback to a second provider, a usage row per call and a price for each.
Open this document when a feature of your bot needs a model: it says how to
turn the layer on, how a use case calls it, and what the layer records.
`/summarize` in `src/note` is the worked example; the snippets below come
from it.

## Turning it on

The layer ships off. With `LLM_PROVIDER` empty the container binds
`DisabledLLMClient`: the bot starts without any key, every use case that
needs a model still resolves, and a call raises `LLMNotConfigured`, which the
user reads as "This feature is not available in this bot."

To turn it on, set in `.env`:

- `LLM_PROVIDER` - `anthropic` or `openrouter`;
- `LLM_MODEL` - the model id, `claude-sonnet-5-5` by default. Anthropic ids
  carry no vendor prefix; OpenRouter ids are `<vendor>/<model>`
  (`google/gemini-3.8-flash`);
- that provider's key, `ANTHROPIC_API_KEY` or `OPENROUTER_API_KEY`.

`.env.example` keeps a working provider and fallback pair in commented lines
under the empty ones. `LLM_TIMEOUT_SECONDS` (per attempt) and
`LLM_MAX_RETRIES` (after a 429, a 5xx or a network error) go to the provider
SDK, so a user waits up to timeout x (retries + 1) for one provider. OpenRouter
requests carry `OPENROUTER_APP_URL` and `OPENROUTER_APP_TITLE` as its
attribution headers when they are set.

`LLMConfig` in `src/main/config.py` refuses to start when a chosen provider,
primary or fallback, has no key or no model, when a model id has the wrong
shape for its provider, when the fallback is the primary provider again, or
when a fallback is set without a primary. Every setting is listed in
[the configuration reference](configuration.md#llm).

## Calling a model

A use case receives `LLMClient` from the container, like a repository, and
never imports a provider SDK. Behind the protocol sit the provider adapters,
the usage recorder and the fallback; the use case cannot tell which it holds,
and every failure reaches it as an `LLMError` subclass from
`src/core/llm/errors.py`.

`LLMClient` has two calls: `complete(request)` returns an `LLMResponse` with
the text, and `structured(request, Model)` returns a `StructuredResponse`
whose `output` is an instance of a pydantic model. An `LLMRequest` carries:

- `purpose` - a domain constant naming the caller (`NOTE_SUMMARY_PURPOSE`),
  at most 64 characters, never text a user typed. It is the key of the usage
  table and of the call's log line;
- `system` and `messages` - the prompt;
- `max_tokens` (1024 by default), `model` (None takes the configured one),
  `temperature` (None sends none; the Anthropic adapter never sends one,
  since the current models reject a non-default value), `cache_system` (asks
  Anthropic to cache a long system prompt reused within minutes).

A malformed request (no purpose, no messages, a purpose too long) raises
`ValueError` when it is built: it is a bug in the caller, not an outage to
retry or fail over.

**Prompts** live in the domain's `prompts.py` (`src/note/prompts.py`), in
English. The reply language is a parameter: `RESPONSE_LANGUAGES` maps every
supported locale to the English name the prompt states, and
`note_summary_system_prompt(locale)` fills it in. Text the user wrote goes
into the user message inside tags, and the system prompt tells the model to
treat it as data.

**The transaction ends before the call.** A model takes seconds, and a pooled
connection must not wait for it. `SummarizeNotesUseCase` reads the notes in a
unit of work, copies the texts inside the block, and calls the model after it.
With no notes it returns before building a request, so an empty case never
costs a paid call:

```python
async with self._uow as uow:
    notes = await self._notes.latest_for_user(uow.session, user_id, SUMMARY_NOTE_LIMIT)
    texts = [note.text for note in notes]
if not texts:
    return None
request = LLMRequest(
    purpose=NOTE_SUMMARY_PURPOSE,
    system=note_summary_system_prompt(locale),
    messages=(LLMMessage(role="user", content=note_summary_user_message(texts)),),
    max_tokens=SUMMARY_MAX_TOKENS,
)
response = await self._llm.structured(request, NoteSummary)
```

**The handler** (`summarize` in `src/note/handlers.py`):

- is registered with `flags={"throttle": Throttle.LLM}`, which gives it the
  tighter `THROTTLE_LLM_LIMIT` per `THROTTLE_LLM_WINDOW_SECONDS` instead of
  the default limit;
- sends `ChatAction.TYPING` first, so the command does not look ignored while
  the model works;
- passes every part of the answer through `html.quote`: the model's text is
  built from what the user typed, and an unescaped `<` would break the HTML
  reply;
- catches nothing. An `LLMError` is a `CoreException`, and the common error
  handler answers with its message: "busy" for `LLMRateLimited`, "took too
  long" for `LLMTimeout`, "declined" for `LLMRefused`, "unavailable" for
  `LLMUnavailable`. A use case turns an error into a domain one where the
  user needs other words: `SummarizeNotesUseCase` raises
  `NoteSummaryFailedError` for `LLMOutputError`.

## Structured output

`structured()` holds the provider to the output model's JSON schema:
Anthropic receives it as `output_config.format`, OpenRouter as a strict
`response_format` (and only providers that honour it are routed to). The
answer is then validated with the pydantic model.

Keep the output model next to its prompt: its field descriptions reach the
model as part of the schema, so they are prompt text too. Put length and
count limits in `field_validator`s, not in `Field(max_length=...)`: strict
JSON schema on some providers refuses a schema that carries them. `NoteSummary`
checks the summary and themes this way.

An answer that fails validation gets one repair request: the validation
errors (locations and messages, never the rejected values) go back to the
model with its answer. A second failure raises `LLMOutputError`. An answer
cut at `max_tokens` is not repaired and raises `LLMOutputError` at once, so
give `max_tokens` room for the longest valid answer; only the tokens produced
are billed. `SUMMARY_MAX_TOKENS` is twice the longest valid summary for that
reason.

## Fallback

With `LLM_FALLBACK_PROVIDER` and `LLM_FALLBACK_MODEL` set, `FallbackLLMClient`
sends each call to the primary and repeats it on the fallback when the
primary is unavailable (`LLMUnavailable`, which also covers a rejected key),
times out (`LLMTimeout`) or is rate limited (`LLMRateLimited`). The fallback
answers with `LLM_FALLBACK_MODEL`: a model named in the request is the
primary vendor's id. A refusal or an unreadable answer does not switch: the
other vendor would most likely repeat it, and be paid for twice.

The fallback must be the other provider, so that one vendor's outage cannot
stop both paths; config refuses the same provider twice.

Every switch logs a warning and queues a developer alert, with the origin
`llm <provider>` naming the provider that failed, before the fallback is
tried, so it leaves even when the fallback fails as well. The alert goes out
in the background and the user's answer does not wait for it; the same
failure is alerted once per `ALERT_COOLDOWN_SECONDS`
([errors and alerts](bot-layer.md#errors-and-alerts)).

## Usage and cost

**One row per attempt.** Every provider call, failed ones included, writes a
row to `llm_usage` (`src/core/llm/models.py`): the Telegram user id, the
purpose, provider and model, token counts (input, output, cache reads and
writes), the cost, latency, the status and the error class. A call that fell
back leaves two rows. A failed call the provider still billed (a refusal, an
answer that failed validation, a repair whose request broke off) is stored
with what it cost. The row is written in a short transaction of its own, so
a call made by a worker task is recorded too, and a failed write is logged
and sent to Sentry without failing the call. There is no foreign key to
`users`: deleting a user keeps what their calls cost.

**Prices.** OpenRouter reports the cost of each call. For Anthropic the cost
comes from the price table in `src/core/llm/pricing.py` (US dollars per
million tokens, as `Decimal`, eight decimal places). `LLM_PRICING_OVERRIDES`
replaces or adds whole entries as JSON; each entry needs all four rates.
A model with no price is recorded with `cost_usd` NULL, which means unknown,
not free, and logs one warning per process naming the model.

**`/stats`.** Admins see spending for today (since midnight UTC), the last 7
and 30 days, and for the last 30 days by purpose and for the top 5 users.
Lines for failed calls and for calls without a known price appear only when
there are any.

**Purge.** The scheduler task `llm_usage.purge` runs every day at 03:30 UTC
and deletes rows older than `LLM_USAGE_RETENTION_DAYS` (90 by default, at
least 30, the longest period `/stats` reports), in batches of 1000.

**What reaches the logs.** Prompts and answers are user data and are never
logged. Each call logs one line: provider, model, purpose, outcome, token
counts, cost and latency. `llm_usage` stores counts only. `LLM_LOG_CONTENT=true`
also logs the prompt and the answer (the error class of a failed call) for
local debugging. The bot refuses to start with it unless `DEBUG=true`, which
turns Sentry off: every log line becomes a Sentry breadcrumb, and the logged
text would leave with the next event. `make check-env` refuses it outright,
so it never reaches a deployment that runs the check.

**Tests** never reach a provider. `FakeLLMClient` from `tests/fakes/llm.py`
answers from a queue and records every request; the adapters are tested
against `ScriptedLLMServer`. Both are described in
[the testing guide](testing.md#fakes-and-helpers).
