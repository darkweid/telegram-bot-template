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
whose `output` is an instance of a pydantic model. An `LLMResponse` holds
`content`: text blocks, tool calls and provider blocks, in the order the model
produced them. `text` joins the text blocks, `tool_calls` lists the calls,
`stop` says why the answer ended (`end`, `tool_use`, `max_tokens`; the
provider's own value stays in `stop_reason`), and `assistant_turn` is the
answer as the next request's history needs it. An `LLMRequest` carries:

- `purpose` - a domain constant naming the caller (`NOTE_SUMMARY_PURPOSE`),
  at most 64 characters, never text a user typed. It is the key of the usage
  table and of the call's log line;
- `system` and `messages` - the prompt;
- `max_tokens` (1024 by default), `model` (None takes the configured one),
  `temperature` (None sends none; the Anthropic adapter never sends one,
  since the current models reject a non-default value), `cache_system` (asks
  Anthropic to cache a long system prompt reused within minutes; it also
  caches the history up to the last message, which pays off in a tool loop.
  On OpenRouter only the system prompt of an `anthropic/*` model is marked,
  the other vendors cache repeated prefixes on their own);
- `tier` - `ModelTier.STRONG` (default) or `ModelTier.FAST`: the domain names
  the kind of work, the configuration names the model (`LLM_MODEL`,
  `LLM_MODEL_FAST`). A fallback keeps the tier and takes its own model of it;
- `effort` - `low`, `medium` or `high`, how hard the model reasons; None
  leaves the model's default. Anthropic gets it in `output_config`,
  OpenRouter as `reasoning.effort`. Keep it fixed across the calls of one
  conversation: a change drops the provider's cache;
- `tools` and `tool_choice` - see [Tools](#tools).

A malformed request (no purpose, no messages, a purpose too long) raises
`ValueError` when it is built: it is a bug in the caller, not an outage to
retry or fail over.

**Prompts** live in the domain's `prompts.py` (`src/note/prompts.py`), in
English. The reply language is a parameter: `RESPONSE_LANGUAGES`
(`src/core/locales.py`) maps every supported locale to the English name the
prompt states, and
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
- sends a status ("⏳ Summarizing your notes…") and runs the use case in
  background work, which edits the status into the summary (see
  [Background work](bot-layer.md#background-work)): the update ends before the
  model answers;
- passes every part of the answer through `html.quote`: the model's text is
  built from what the user typed, and an unescaped `<` would break the HTML
  reply;
- catches nothing. An `LLMError` is a `CoreException`; leaving the status
  block with it edits the status into its message, the same text the error
  handler of an update sends: "busy" for `LLMRateLimited`, "took too
  long" for `LLMTimeout`, "declined" for `LLMRefused`, "unavailable" for
  `LLMUnavailable`. A use case turns an error into a domain one where the
  user needs other words: `SummarizeNotesUseCase` raises
  `NoteSummaryFailedError` for `LLMOutputError`.

## Model tiers

A request names the kind of work with `tier`, not a model id.
`ModelTier.STRONG` is the default and uses `LLM_MODEL`; `ModelTier.FAST`
uses `LLM_MODEL_FAST` and suits cheap, frequent calls such as routing or
short parsing. An empty `LLM_MODEL_FAST` means the strong model of the same
provider. Each `llm_usage` row stores the model: the one that answered, or
the one asked for when the call failed. `/stats` groups spending by purpose
and by user, not by model or tier.

## Effort

`effort` (`low`, `medium`, `high`) sets how hard the model reasons before it
answers; None sends nothing and leaves the model's default. Anthropic gets it
as `output_config.effort`, OpenRouter as `reasoning.effort`. A higher effort
costs output tokens, so raise `max_tokens` with it. The provider's cache is
keyed by the request prefix, so keep the value fixed across the calls of one
conversation.

## Structured output

`structured()` holds the provider to the output model's JSON schema:
Anthropic receives it as `output_config.format`, OpenRouter as a strict
`response_format`, and OpenRouter routes the call only to providers that
support it. The answer is then validated with the pydantic model.

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

## Tools

A tool is an `LLMTool(name, description, args)`, where `args` is a pydantic
model. Both APIs get its schema in strict mode, so every field is required
(an optional one is `T | None` with no default) and no other key is
accepted; put length limits in `field_validator`s, as for structured output.
Names match `^[a-zA-Z0-9_-]{1,64}$` and are unique within a request.

The model answers with `stop == "tool_use"` and one or more `ToolUseBlock`s
in `response.tool_calls`; Anthropic is asked for one call per answer, while
OpenRouter may return several. The caller validates each call's `input` with
`args.model_validate`, runs the tool and sends the result back in the next
request: the history gains `response.assistant_turn` unchanged and a user
message of `ToolResultBlock`s, one per call, with `is_error=True` for a call
that failed, so the model can correct itself. `tool_choice="none"` keeps the
tools in the prompt and forbids calls.

`run` below is the domain's tool dispatcher: it finds the tool by
`call.name`, validates `call.input` with `tool.args.model_validate` and
returns the result text.

```python
response = await llm.complete(request)
if response.stop == "tool_use":
    results = [
        ToolResultBlock(tool_use_id=call.id, content=await run(call))
        for call in response.tool_calls
    ]
    request = replace(
        request,
        messages=[*request.messages, response.assistant_turn,
                  LLMMessage(role="user", content=results)],
    )
    response = await llm.complete(request)
```

A tool call cut at `max_tokens` raises `LLMOutputError`: its arguments are
incomplete. Models that reason spend output tokens on it, so a request with
tools needs a `max_tokens` well above the longest answer. `structured()`
takes no tools and refuses a request that has them with `ValueError`.
A call whose arguments are not a JSON object raises `LLMUnavailable`, which
still records what the answer cost.

An answer that carries tool calls reads `stop == "tool_use"` even when a
provider behind OpenRouter reports `stop`; `stop_reason` keeps the raw
value. Both adapters send a user turn's tool results ahead of its text and
images, as the APIs require, and leave out empty text blocks.

**Provider blocks.** Anthropic signs its thinking and OpenRouter returns
`reasoning_details`; both arrive as `ProviderBlock`s and must go back in the
history unchanged, which `assistant_turn` does. An adapter drops the other
provider's blocks.

`run_agent` (see [Agent loop](#agent-loop)) runs this loop for you; write it
by hand only when the loop does not fit.

## Agent loop

`run_agent` (`src/core/llm/agent.py`) asks the model, runs the tools it
calls and sends the results back, until the model answers in text, a tool
ends the run, the steps run out or the deadline passes:

```python
run = await run_agent(
    llm,
    LLMRequest(purpose=PANTRY_PURPOSE, system=system, messages=history),
    [FindItem(repository), StoreItem(repository)],
    max_steps=6,
    deadline_seconds=40,
    on_step=partial(show_step, status),
)
```

A tool is any object with `name`, `description`, `args` (a pydantic model,
under the rules of [Tools](#tools)) and `async run(args) -> ToolResult`.
`ToolResult.content` goes back to the model; `payload` is the domain's own
value, collected in `run.payloads` in call order; `ends_run=True` stops the
loop after the current batch of calls; `wrote=True` says the tool changed
stored data.

- The base request carries the prompt, model, tier, effort and limits, and no
  tools. They stay the same on every step, so the provider's prompt cache
  keeps working. Tools in the base request, `max_steps` below 1, a deadline
  that is not positive and two tools with one name raise `ValueError`.
- The calls of one answer run in order and their results go back in one
  message. Arguments that fail validation, a tool that does not exist and a
  `CoreException` from a tool go back to the model as an error result (the
  exception's English text with its params, never its `detail`), so the
  model can correct itself. Any other exception from a tool or from
  `on_step` ends the run and is raised as it is.
- `deadline_seconds` bounds every model call and every tool, not `on_step`.
  A tool the deadline cut counts as `wrote`, since it may have written; it
  and the calls after it get a "not run" result. A cancel from outside, a stopping
  bot, is raised as a cancel, never turned into a deadline.
- `AgentRun.stop` is `answer`, `tool_ended`, `steps_exhausted` or `deadline`;
  `final_text` is the last answer's text, `messages` the history with every
  turn the loop added, the final answer included, `usage` the sum of every
  call (`cost_usd` None when any cost was unknown, a model call the deadline
  cut included: its `llm_usage` row has no cost). The caller decides what
  the user hears: after `deadline` or `steps_exhausted` with `wrote`, part of
  a change may be saved.
- An `LLMError` ends the run and carries the run so far in
  `error.agent_run`, so the caller can still tell whether a tool wrote.
- `on_step(StepEvent(step, tool_names))` runs before the step's tools: show
  progress there, with `ProcessingStatus.update`.

Each run logs one line, however it ends: purpose, steps, outcome (the stop,
or the class of the exception that ended it, a cancel included), the tools
called with their counts, whether a tool wrote, and the cost; never the
prompt, the arguments or the answers.

A run takes seconds: start it in [background work](bot-layer.md#background-work),
never in the handler. `tests/unit/src/core/test_agent_in_background.py` is
the worked example: the handler sends a status and starts the work, the work
runs the loop, shows each step and edits the status into the answer.

## Images

`ImageBlock(media_type, data)` goes into a user message next to text:
JPEG, PNG, GIF or WebP, at most 3,932,160 bytes (`MAX_IMAGE_BYTES`), so
that even the base64 form, 4/3 of the bytes, stays under Anthropic's 5 MiB
limit. Anything else raises `ValueError` when the block is built; shrinking
a photo is the domain's job. The log under `LLM_LOG_CONTENT` names an image by type and
size, never its bytes.

## Fallback

With `LLM_FALLBACK_PROVIDER` and `LLM_FALLBACK_MODEL` set, `FallbackLLMClient`
sends each call to the primary and repeats it on the fallback when the
primary is unavailable (`LLMUnavailable`, which also covers a rejected key),
times out (`LLMTimeout`) or is rate limited (`LLMRateLimited`). The fallback
answers with `LLM_FALLBACK_MODEL`: a model named in the request is the
primary vendor's id. A refusal or an unreadable answer does not switch: the
other vendor would most likely repeat it, and be paid for twice.

The fallback answers at the request's tier with its own models
(`LLM_FALLBACK_MODEL`, `LLM_FALLBACK_MODEL_FAST`). A history with tool calls
goes over as it is, minus the primary's provider blocks; on a model that
reasons, a switch in the middle of a tool loop may still be refused, since
the last assistant turn lost its own reasoning. That surfaces as
`LLMUnavailable` from the fallback.

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

**Cancelled calls.** A call its caller cut short (the agent's deadline, a
stopping bot) is a row too: status error, error class `Cancelled`, no tokens
and no cost, whatever the provider billed; its log line reads `Cancelled`.
The row gets 2 seconds to be written and is dropped after that, so a
stopping bot is not held up. A call that had already ended keeps its own
row when the cancel lands while that row is being written: the write is
retried under the same 2 seconds, with the tokens and cost the call
reported.

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
