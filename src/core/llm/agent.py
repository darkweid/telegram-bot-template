"""The agent loop: the model asks for tools, the tools answer, until the model
answers in text, a tool ends the run, the steps run out or the deadline passes.

Knows nothing of Telegram: a domain runs it inside background work
(src/core/telegram/background.py) and shows progress through `on_step`.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from loggers import get_logger
from src.core.errors.exceptions import CoreException
from src.core.llm.errors import LLMError
from src.core.llm.interface import LLMClient
from src.core.llm.structured import combine_usage
from src.core.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMTool,
    LLMUsage,
    ToolResultBlock,
    ToolUseBlock,
)

logger = get_logger(__name__)

NOT_RUN_TEXT = "Not run: the time limit was reached."
# The name the log line counts a call to a tool that does not exist under:
# the name itself is model output.
UNKNOWN_TOOL = "<unknown>"


class ToolResult(BaseModel):
    """`content` goes back to the model. `payload` is the domain's own value,
    collected in call order and never read by the loop. `wrote` says the
    tool changed stored data."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    content: str
    ends_run: bool = False
    payload: object | None = None
    wrote: bool = False


class Tool[ArgsT: BaseModel](Protocol):
    """A tool the loop runs. `args` validates the model's input before `run`
    sees it. A CoreException from `run` goes back to the model as its English
    text; any other exception ends the run."""

    name: str
    description: str
    args: type[ArgsT]

    async def run(self, args: ArgsT) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class StepEvent:
    step: int
    tool_names: tuple[str, ...]


type AgentStop = Literal["answer", "tool_ended", "steps_exhausted", "deadline"]


@dataclass(frozen=True, slots=True)
class AgentRun:
    """`messages` is the base request's history plus every turn the loop
    added, the final answer included, so a caller can continue from it.
    `wrote` is True when a tool wrote, or may have: a tool cut by the
    deadline counts."""

    messages: tuple[LLMMessage, ...]
    final_text: str
    payloads: tuple[object, ...]
    wrote: bool
    usage: LLMUsage
    stop: AgentStop
    steps: int


async def run_agent(
    client: LLMClient,
    base: LLMRequest,
    tools: Sequence[Tool[Any]],
    *,
    max_steps: int,
    deadline_seconds: float,
    on_step: Callable[[StepEvent], Awaitable[None]] | None = None,
) -> AgentRun:
    """Run the loop on `base`, whose system prompt, model, tier, effort and
    limits stay the same on every step: a change would reset the provider's
    prompt cache.

    `deadline_seconds` bounds every model call and every tool, not `on_step`.
    An LLMError is raised with the run so far in its `agent_run`; any other
    exception from a tool or from `on_step` is raised as it is, and so is a
    cancel from outside. A model call cut by the deadline makes
    `usage.cost_usd` unknown.
    """
    if base.tools:
        raise ValueError("tools go to run_agent, not to the base request")
    if max_steps < 1:
        raise ValueError("max_steps must be at least 1")
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    by_name = {tool.name: tool for tool in tools}
    if len(by_name) != len(tools):
        raise ValueError("tools share a name")
    loop = asyncio.get_running_loop()
    run = _Run(base, by_name, deadline=loop.time() + deadline_seconds)
    try:
        result = await run.drive(client, max_steps, on_step)
    except LLMError as error:
        error.agent_run = run.result("answer", "")
        run.log(type(error).__name__)
        raise
    except BaseException as error:
        # A cancel or a bug in a tool or on_step: the line still says how
        # far the run got.
        run.log(type(error).__name__)
        raise
    run.log(result.stop)
    return result


class _Run:
    def __init__(
        self, base: LLMRequest, tools: Mapping[str, Tool[Any]], *, deadline: float
    ) -> None:
        self._base = base
        self._tools = tools
        self._llm_tools = tuple(
            LLMTool(name=tool.name, description=tool.description, args=tool.args)
            for tool in tools.values()
        )
        self._deadline = deadline
        self._messages: list[LLMMessage] = list(base.messages)
        self._payloads: list[object] = []
        self._wrote = False
        self._usage = LLMUsage(input_tokens=0, output_tokens=0, cost_usd=Decimal(0))
        self._steps = 0
        self._tool_calls: Counter[str] = Counter()

    def result(self, stop: AgentStop, final_text: str) -> AgentRun:
        return AgentRun(
            messages=tuple(self._messages),
            final_text=final_text,
            payloads=tuple(self._payloads),
            wrote=self._wrote,
            usage=self._usage,
            stop=stop,
            steps=self._steps,
        )

    def log(self, stop: str) -> None:
        cost = self._usage.cost_usd
        logger.info(
            "Agent run purpose=%s steps=%d stop=%s tools=%s wrote=%s cost_usd=%s",
            self._base.purpose,
            self._steps,
            stop,
            ",".join(f"{name}:{n}" for name, n in sorted(self._tool_calls.items()))
            or "-",
            self._wrote,
            "unknown" if cost is None else cost,
        )

    def _expired(self) -> bool:
        return asyncio.get_running_loop().time() >= self._deadline

    async def drive(
        self,
        client: LLMClient,
        max_steps: int,
        on_step: Callable[[StepEvent], Awaitable[None]] | None,
    ) -> AgentRun:
        last_text = ""
        while True:
            # Checked first: a call made past the deadline is paid for and
            # cancelled at once.
            if self._expired():
                return self.result("deadline", last_text)
            response = await self._call_model(client)
            if response is None:
                return self.result("deadline", last_text)
            last_text = response.text
            self._messages.append(response.assistant_turn)
            calls = response.tool_calls
            if not calls:
                return self.result("answer", response.text)
            if on_step is not None:
                await on_step(StepEvent(self._steps, tuple(c.name for c in calls)))
            outcome = await self._run_batch(calls)
            if outcome == "deadline":
                return self.result("deadline", response.text)
            if outcome == "ended":
                return self.result("tool_ended", response.text)
            if self._steps >= max_steps:
                return self.result("steps_exhausted", response.text)

    async def _call_model(self, client: LLMClient) -> LLMResponse | None:
        """None when the deadline cut the call."""
        request = replace(
            self._base, messages=tuple(self._messages), tools=self._llm_tools
        )
        self._steps += 1
        timeout = asyncio.timeout_at(self._deadline)
        try:
            async with timeout:
                response = await client.complete(request)
        except TimeoutError:
            # A TimeoutError of the client's own is not the deadline.
            if timeout.expired():
                # Its usage row has no cost, and the provider may have
                # billed it: the total is unknown, not lower.
                self._usage = replace(self._usage, cost_usd=None)
                return None
            raise
        except LLMError as error:
            if error.usage is not None:
                self._usage = combine_usage(self._usage, error.usage)
            raise
        self._usage = combine_usage(self._usage, response.usage)
        return response

    async def _run_batch(
        self, calls: Sequence[ToolUseBlock]
    ) -> Literal["next", "ended", "deadline"]:
        """Every call of one answer runs, in order, even after one ended the
        run: the model expects a result for each call id."""
        results: list[ToolResultBlock] = []
        ended = cut = False
        for call in calls:
            self._tool_calls[
                call.name if call.name in self._tools else UNKNOWN_TOOL
            ] += 1
            if cut or self._expired():
                cut = True
                results.append(_not_run(call))
                continue
            block, result = await self._run_tool(call)
            if block is None:
                # The tool may have written before it was cut: the caller
                # decides what to tell the user.
                cut = self._wrote = True
                results.append(_not_run(call))
                continue
            results.append(block)
            if result is not None:
                if result.payload is not None:
                    self._payloads.append(result.payload)
                self._wrote = self._wrote or result.wrote
                ended = ended or result.ends_run
        self._messages.append(LLMMessage(role="user", content=tuple(results)))
        if cut:
            return "deadline"
        return "ended" if ended else "next"

    async def _run_tool(
        self, call: ToolUseBlock
    ) -> tuple[ToolResultBlock | None, ToolResult | None]:
        """(None, None) when the deadline cut the tool."""
        tool = self._tools.get(call.name)
        if tool is None:
            names = ", ".join(self._tools)
            text = f'Unknown tool "{call.name}". Available tools: {names}.'
            return _error(call, text), None
        try:
            args = tool.args.model_validate(call.input)
        except ValidationError as error:
            # The same shape as the repair message of structured(), without
            # the values the model sent.
            text = error.json(
                include_url=False, include_context=False, include_input=False
            )
            return _error(call, text), None
        timeout = asyncio.timeout_at(self._deadline)
        try:
            async with timeout:
                result = await tool.run(args)
        except TimeoutError:
            if timeout.expired():
                return None, None
            raise
        except CoreException as error:
            logger.info("Tool %s failed: %s", tool.name, type(error).__name__)
            return _error(call, _english_text(error)), None
        return ToolResultBlock(tool_use_id=call.id, content=result.content), result


def _error(call: ToolUseBlock, text: str) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=call.id, content=text, is_error=True)


def _not_run(call: ToolUseBlock) -> ToolResultBlock:
    return _error(call, NOT_RUN_TEXT)


def _english_text(error: CoreException) -> str:
    """The msgid with its params: the model reads English, and `detail` may
    carry internals it must not see."""
    try:
        return error.message_key.format(**error.params)
    except (KeyError, IndexError, AttributeError, ValueError, TypeError):
        return error.message_key
