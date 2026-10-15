import asyncio
from decimal import Decimal

from pydantic import BaseModel
import pytest

from src.core.errors.exceptions import CoreException
from src.core.i18n import N_
from src.core.llm.agent import NOT_RUN_TEXT, StepEvent, ToolResult, run_agent
from src.core.llm.enums import LLMProviderName
from src.core.llm.errors import LLMUnavailable
from src.core.llm.types import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMTool,
    LLMUsage,
    ProviderBlock,
    TextBlock,
    ToolResultBlock,
)
from tests.fakes.llm import FakeLLMClient, llm_response, tool_call
from tests.helpers.logs import captured_logs

BASE = LLMRequest(
    purpose="test_agent",
    messages=(LLMMessage(role="user", content="What is in my pantry?"),),
    system="You answer from the pantry.",
    effort="low",
)


class LookupArgs(BaseModel):
    query: str


class Lookup:
    """Records what it was asked; answers with a payload when given one."""

    name = "lookup"
    description = "Finds an item in the pantry."
    args = LookupArgs

    def __init__(
        self, *, ends_run: bool = False, wrote: bool = False, payload: object = None
    ) -> None:
        self.queries: list[str] = []
        self._ends_run = ends_run
        self._wrote = wrote
        self._payload = payload

    async def run(self, args: LookupArgs) -> ToolResult:
        self.queries.append(args.query)
        return ToolResult(
            content=f"found {args.query}",
            ends_run=self._ends_run,
            wrote=self._wrote,
            payload=self._payload,
        )


class SaveArgs(BaseModel):
    item: str


class Save:
    name = "save"
    description = "Stores an item."
    args = SaveArgs

    def __init__(self) -> None:
        self.items: list[str] = []

    async def run(self, args: SaveArgs) -> ToolResult:
        self.items.append(args.item)
        return ToolResult(content="saved", wrote=True, payload=args.item)


class Hangs:
    """A tool that never returns on its own."""

    name = "hangs"
    description = "Waits forever."
    args = LookupArgs

    def __init__(self) -> None:
        self.cancelled = False

    async def run(self, args: LookupArgs) -> ToolResult:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


class Raises:
    name = "raises"
    description = "Fails."
    args = LookupArgs

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def run(self, args: LookupArgs) -> ToolResult:
        raise self._error


class NotInStock(CoreException):
    message_key = N_("{item} is not in stock")
    log_level = "info"


class HangingClient(FakeLLMClient):
    """Answers from the queue, then hangs on the next call."""

    def __init__(self, answers: int) -> None:
        super().__init__()
        self._answers = answers
        self.entered = asyncio.Event()
        self.cancelled = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if len(self.calls) < self._answers:
            return await super().complete(request)
        self.calls.append(request)
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("unreachable")


def _results(message: LLMMessage) -> list[ToolResultBlock]:
    assert message.role == "user" and not isinstance(message.content, str)
    blocks = list(message.content)
    assert all(isinstance(block, ToolResultBlock) for block in blocks)
    return [block for block in blocks if isinstance(block, ToolResultBlock)]


async def test_an_answer_without_tool_calls_ends_the_run() -> None:
    """A model that needs no tool has answered: one call, its text."""
    llm = FakeLLMClient()
    llm.queue(llm_response("Rice and beans."))
    run = await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    assert (run.stop, run.final_text, run.steps) == ("answer", "Rice and beans.", 1)
    assert len(llm.calls) == 1


async def test_the_history_grows_by_each_turn_and_one_result_message_per_batch() -> (
    None
):
    """The provider rejects a tool result without the call it answers, and
    a caller continuing the conversation needs the final answer too."""
    llm = FakeLLMClient()
    call = tool_call("lookup", {"query": "rice"})
    llm.queue_tool_calls(call, text="Let me look.")
    llm.queue(llm_response("You have rice."))
    run = await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    assert run.messages[0] == BASE.messages[0]
    first, results, answer = run.messages[1:]
    assert first.role == "assistant"
    assert [b.tool_use_id for b in _results(results)] == [call.id]
    assert answer == LLMMessage(
        role="assistant", content=(TextBlock("You have rice."),)
    )
    assert llm.calls[1].messages == run.messages[:3]


async def test_provider_blocks_go_back_unchanged() -> None:
    """Anthropic refuses a continuation whose thinking block was altered."""
    llm = FakeLLMClient()
    thinking = ProviderBlock(
        provider=LLMProviderName.ANTHROPIC, data={"type": "thinking", "x": 1}
    )
    call = tool_call("lookup", {"query": "rice"})
    llm.queue(
        llm_response(content=(thinking, call), stop="tool_use", stop_reason="tool_use")
    )
    llm.queue(llm_response("Done."))
    await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    assert llm.calls[1].messages[1].content == (thinking, call)


async def test_several_calls_in_one_answer_run_in_order_with_one_result_message() -> (
    None
):
    """Each result answers its own call id, in the order the model asked."""
    llm = FakeLLMClient()
    lookup = Lookup()
    first = tool_call("lookup", {"query": "rice"})
    second = tool_call("lookup", {"query": "beans"})
    llm.queue_tool_calls(first, second)
    llm.queue(llm_response("Both."))
    run = await run_agent(llm, BASE, [lookup], max_steps=5, deadline_seconds=5)
    assert lookup.queries == ["rice", "beans"]
    results = _results(run.messages[2])
    assert [(b.tool_use_id, b.content) for b in results] == [
        (first.id, "found rice"),
        (second.id, "found beans"),
    ]


async def test_a_tool_that_ends_the_run_ends_it_after_its_batch() -> None:
    """The model expects a result for every call it made; the run stops
    only after the whole batch, without another model call."""
    llm = FakeLLMClient()
    ender = Lookup(ends_run=True, payload="proposal")
    save = Save()
    llm.queue_tool_calls(
        tool_call("lookup", {"query": "rice"}),
        tool_call("save", {"item": "beans"}),
        text="Here is my proposal.",
    )
    run = await run_agent(llm, BASE, [ender, save], max_steps=5, deadline_seconds=5)
    assert run.stop == "tool_ended"
    assert run.final_text == "Here is my proposal."
    assert save.items == ["beans"]
    assert run.payloads == ("proposal", "beans")
    assert len(llm.calls) == 1


async def test_payloads_and_wrote_accumulate_across_steps() -> None:
    """The domain reads every payload after the run, in call order, and
    must know whether anything was written."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("save", {"item": "rice"}))
    llm.queue_tool_calls(tool_call("lookup", {"query": "rice"}))
    llm.queue(llm_response("Saved."))
    run = await run_agent(
        llm, BASE, [Save(), Lookup(payload="seen")], max_steps=5, deadline_seconds=5
    )
    assert run.payloads == ("rice", "seen")
    assert run.wrote


async def test_steps_run_out() -> None:
    """A model that keeps asking for tools is stopped; the caller answers
    from what it has."""
    llm = FakeLLMClient()
    for query in ("a", "b", "c"):
        llm.queue_tool_calls(tool_call("lookup", {"query": query}), text=query)
    run = await run_agent(llm, BASE, [Lookup()], max_steps=2, deadline_seconds=5)
    assert (run.stop, run.steps, run.final_text) == ("steps_exhausted", 2, "b")
    assert len(llm.calls) == 2


async def test_on_step_hears_each_step_before_its_tools_run() -> None:
    """The status shows what the bot is doing before a slow tool starts."""
    llm = FakeLLMClient()
    lookup = Lookup()
    heard: list[tuple[StepEvent, list[str]]] = []

    async def on_step(event: StepEvent) -> None:
        heard.append((event, list(lookup.queries)))

    llm.queue_tool_calls(tool_call("lookup", {"query": "a"}))
    llm.queue_tool_calls(
        tool_call("lookup", {"query": "b"}), tool_call("save", {"item": "c"})
    )
    llm.queue(llm_response("Done."))
    await run_agent(
        llm, BASE, [lookup, Save()], max_steps=5, deadline_seconds=5, on_step=on_step
    )
    assert heard == [
        (StepEvent(step=1, tool_names=("lookup",)), []),
        (StepEvent(step=2, tool_names=("lookup", "save")), ["a"]),
    ]


async def test_invalid_arguments_go_back_as_an_error_without_the_input() -> None:
    """The model can fix its call from the error; the value it sent may be
    user text and stays out of the history it is shown again."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("lookup", {"query": 7, "note": "secret-input"}))
    llm.queue(llm_response("Sorry."))
    lookup = Lookup()
    run = await run_agent(llm, BASE, [lookup], max_steps=5, deadline_seconds=5)
    [result] = _results(run.messages[2])
    assert result.is_error
    assert "query" in result.content
    assert "secret-input" not in result.content and "7" not in result.content
    assert lookup.queries == []


async def test_a_core_exception_from_a_tool_goes_back_as_its_message() -> None:
    """An expected failure is information for the model, in English, with
    its params; the run goes on."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("raises", {"query": "rice"}))
    llm.queue(llm_response("Rice is out."))
    tool = Raises(NotInStock("internal detail", item="Rice"))
    with captured_logs("src.core.llm.agent") as records:
        run = await run_agent(llm, BASE, [tool], max_steps=5, deadline_seconds=5)
    [result] = _results(run.messages[2])
    assert (result.is_error, result.content) == (True, "Rice is not in stock")
    assert run.stop == "answer"
    assert any(
        "raises" in r.getMessage() and "NotInStock" in r.getMessage() for r in records
    )
    assert not any("internal detail" in r.getMessage() for r in records)


async def test_an_unknown_tool_goes_back_as_an_error_naming_the_tools() -> None:
    """A model that invents a tool learns which ones exist."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("delete_everything", {}))
    llm.queue(llm_response("Sorry."))
    run = await run_agent(
        llm, BASE, [Lookup(), Save()], max_steps=5, deadline_seconds=5
    )
    [result] = _results(run.messages[2])
    assert result.is_error
    assert result.content == (
        'Unknown tool "delete_everything". Available tools: lookup, save.'
    )


async def test_any_other_tool_error_ends_the_run() -> None:
    """A bug in a tool is not the model's to work around."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("raises", {"query": "rice"}))
    with pytest.raises(ZeroDivisionError):
        await run_agent(
            llm, BASE, [Raises(ZeroDivisionError())], max_steps=5, deadline_seconds=5
        )


async def test_the_deadline_cuts_a_slow_model_call() -> None:
    """The run must end inside the time the bot has, a hung provider
    included."""
    llm = HangingClient(answers=1)
    llm.queue_tool_calls(tool_call("lookup", {"query": "rice"}), text="Looking.")
    run = await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=0.05)
    assert (run.stop, run.steps, run.final_text) == ("deadline", 2, "Looking.")
    assert llm.cancelled


async def test_the_deadline_cuts_a_slow_tool() -> None:
    """The cut tool and the ones after it answer "not run", so the history
    stays valid; the cut one may have written, so the run says it did."""
    llm = FakeLLMClient()
    hangs = Hangs()
    save = Save()
    first = tool_call("hangs", {"query": "rice"})
    second = tool_call("save", {"item": "beans"})
    llm.queue_tool_calls(first, second)
    run = await run_agent(llm, BASE, [hangs, save], max_steps=5, deadline_seconds=0.05)
    assert run.stop == "deadline"
    assert hangs.cancelled and save.items == []
    assert run.wrote
    results = _results(run.messages[-1])
    assert [(b.tool_use_id, b.content, b.is_error) for b in results] == [
        (first.id, NOT_RUN_TEXT, True),
        (second.id, NOT_RUN_TEXT, True),
    ]


async def test_a_timeout_raised_inside_a_tool_is_not_the_deadline() -> None:
    """A tool's own TimeoutError is a failure of the tool, not of the run's
    time budget."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("raises", {"query": "rice"}))
    with pytest.raises(TimeoutError):
        await run_agent(
            llm, BASE, [Raises(TimeoutError())], max_steps=5, deadline_seconds=5
        )


async def test_an_llm_error_carries_the_partial_run() -> None:
    """A failure after a tool wrote must not read as "nothing happened": the
    caller sees what was written and what was spent."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("save", {"item": "rice"}))
    billed = LLMUsage(input_tokens=3, output_tokens=1, cost_usd=Decimal("0.00000100"))
    llm.queue(LLMUnavailable(usage=billed))
    with pytest.raises(LLMUnavailable) as caught:
        await run_agent(llm, BASE, [Save()], max_steps=5, deadline_seconds=5)
    partial = caught.value.agent_run
    assert partial is not None
    assert (partial.stop, partial.final_text, partial.steps) == ("answer", "", 2)
    assert partial.wrote and partial.payloads == ("rice",)
    assert partial.usage.input_tokens == 10 + 3
    assert partial.usage.cost_usd == Decimal("0.00010000") + Decimal("0.00000100")


async def test_usage_sums_every_call_and_unknown_cost_stays_unknown() -> None:
    """One unpriced call makes the total unknown rather than too low."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("lookup", {"query": "rice"}))
    llm.queue(llm_response("Done.", input_tokens=20, output_tokens=7, cost_usd=None))
    run = await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    assert (run.usage.input_tokens, run.usage.output_tokens) == (30, 12)
    assert run.usage.cost_usd is None


async def test_system_tools_and_effort_stay_the_same_on_every_step() -> None:
    """Any change to them resets the provider's prompt cache."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(tool_call("lookup", {"query": "rice"}))
    llm.queue(llm_response("Done."))
    await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    first, second = llm.calls
    for request in (first, second):
        assert request.system == BASE.system
        assert request.effort == BASE.effort
        assert request.tool_choice == "auto"
        assert request.max_tokens == BASE.max_tokens
    assert first.tools == second.tools
    assert [tool.name for tool in first.tools] == ["lookup"]
    assert isinstance(first.tools[0], LLMTool) and first.tools[0].args is LookupArgs


async def test_a_base_request_with_tools_is_refused() -> None:
    """Tools given twice could disagree; the loop's list is the one it runs."""
    base = LLMRequest(
        purpose="test_agent",
        messages=BASE.messages,
        tools=(LLMTool(name="lookup", description="x", args=LookupArgs),),
    )
    with pytest.raises(ValueError, match="tools"):
        await run_agent(
            FakeLLMClient(), base, [Lookup()], max_steps=1, deadline_seconds=1
        )


@pytest.mark.parametrize(("max_steps", "deadline"), [(0, 1.0), (1, 0.0), (1, -1.0)])
async def test_bad_limits_are_refused(max_steps: int, deadline: float) -> None:
    """A run without steps or time would end before it began."""
    with pytest.raises(ValueError):
        await run_agent(
            FakeLLMClient(),
            BASE,
            [Lookup()],
            max_steps=max_steps,
            deadline_seconds=deadline,
        )


async def test_duplicate_tool_names_are_refused() -> None:
    """The model calls tools by name; two with one name are ambiguous."""
    with pytest.raises(ValueError, match="share a name"):
        await run_agent(
            FakeLLMClient(), BASE, [Lookup(), Lookup()], max_steps=1, deadline_seconds=1
        )


async def test_a_run_logs_one_line_without_content() -> None:
    """Prompts, answers and tool input are user data; the line carries counts."""
    llm = FakeLLMClient()
    llm.queue_tool_calls(
        tool_call("lookup", {"query": "secret-query"}), text="secret-a"
    )
    llm.queue(llm_response("secret-answer"))
    with captured_logs("src.core.llm.agent") as records:
        await run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    [record] = records
    line = record.getMessage()
    assert "purpose=test_agent" in line and "steps=2" in line
    assert "stop=answer" in line and "tools=lookup:1" in line
    assert "secret" not in line and "pantry" not in line


async def test_a_cancel_from_outside_is_not_a_deadline() -> None:
    """A stopping bot cancels the run; it must not read as a finished one."""
    llm = HangingClient(answers=0)
    task = asyncio.create_task(
        run_agent(llm, BASE, [Lookup()], max_steps=5, deadline_seconds=5)
    )
    async with asyncio.timeout(2):
        await llm.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert llm.cancelled
