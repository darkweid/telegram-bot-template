"""How a domain runs the agent loop: the handler sends a status and starts
background work, the work runs the loop, shows each step and edits the
status into the answer. The template has no domain with tools, so this test
is the worked example."""

import asyncio
from functools import partial

from aiogram import html
from aiogram.methods import EditMessageText, SendMessage
from aiogram.utils.i18n import I18n
from dishka import AsyncContainer
from pydantic import BaseModel
import pytest

from src.core.llm.agent import StepEvent, Tool, ToolResult, run_agent
from src.core.llm.interface import LLMClient
from src.core.llm.types import LLMMessage, LLMRequest
from src.core.telegram import status as status_module
from src.core.telegram.background import BackgroundRunner
from src.core.telegram.status import ProcessingStatus
from src.main.config import Config
from tests.fakes.llm import FakeLLMClient, llm_response, tool_call
from tests.helpers.dispatcher import RunningBot, running_bot

CHAT_ID = 42
BASE = LLMRequest(
    purpose="example_agent",
    messages=(LLMMessage(role="user", content="Add rice to my pantry."),),
    system="You keep the user's pantry.",
)


class ItemArgs(BaseModel):
    item: str


class Find:
    """Waits for `release` when one is given."""

    name = "find"
    description = "Finds an item in the pantry."
    args = ItemArgs

    def __init__(self, release: asyncio.Event | None = None) -> None:
        self._release = release

    async def run(self, args: ItemArgs) -> ToolResult:
        if self._release is not None:
            await self._release.wait()
        return ToolResult(content=f"{args.item}: not stored")


class Store:
    """Writes, then waits for `release` when one is given."""

    name = "store"
    description = "Stores an item in the pantry."
    args = ItemArgs

    def __init__(self, release: asyncio.Event | None = None) -> None:
        self.started = asyncio.Event()
        self._release = release

    async def run(self, args: ItemArgs) -> ToolResult:
        self.started.set()
        if self._release is not None:
            await self._release.wait()
        return ToolResult(content="stored", wrote=True)


async def show_step(status: ProcessingStatus, event: StepEvent) -> None:
    status.update(f"Step {event.step}: {', '.join(event.tool_names)}")


async def answer_with_tools(
    container: AsyncContainer,
    *,
    status: ProcessingStatus,
    tools: list[Tool[ItemArgs]],
    deadline_seconds: float,
) -> None:
    async with status:
        llm = await container.get(LLMClient)
        run = await run_agent(
            llm,
            BASE,
            tools,
            max_steps=4,
            deadline_seconds=deadline_seconds,
            on_step=partial(show_step, status),
        )
        if run.stop == "answer":
            await status.finish(html.quote(run.final_text))
        elif run.wrote:
            # The caller decides what a cut run means: a write may have landed.
            await status.finish("Out of time; part of the change may be saved.")
        else:
            await status.finish("Out of time; nothing was changed.")


async def _start(
    running: RunningBot, i18n: I18n, tools: list[Tool[ItemArgs]], deadline: float
) -> None:
    """What the handler does: the status, then the work."""
    with i18n.context():
        status = await ProcessingStatus.send(running.bot, CHAT_ID, i18n=i18n)
        runner = await running.container.get(BackgroundRunner)
        runner.start(
            "example.agent",
            partial(
                answer_with_tools,
                status=status,
                tools=tools,
                deadline_seconds=deadline,
            ),
        )


@pytest.fixture(autouse=True)
def fast_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status_module, "EDIT_INTERVAL_SECONDS", 0)


async def test_each_step_shows_in_the_status_and_the_answer_replaces_it(
    settings: Config, i18n: I18n
) -> None:
    async with running_bot(settings, i18n) as running:
        llm = await running.container.get(LLMClient)
        assert isinstance(llm, FakeLLMClient)
        llm.queue_tool_calls(tool_call("find", {"item": "rice"}))
        llm.queue_tool_calls(tool_call("store", {"item": "rice"}))
        llm.queue(llm_response("Rice <1 kg> added."))
        release = asyncio.Event()
        await _start(running, i18n, [Find(release), Store()], deadline=5)
        # The step shows while its tool is still running.
        await running.session.wait_for(EditMessageText)
        release.set()
        await running.settle()
        edits = [edit.text for edit in running.session.sent(EditMessageText)]
    assert [m.text for m in running.session.sent(SendMessage)] == ["⏳ Working on it…"]
    assert edits[0] == "⏳ Step 1: find"
    assert edits[-1] == "Rice &lt;1 kg&gt; added."
    assert len(llm.calls) == 3


async def test_a_deadline_during_a_tool_gives_the_callers_answer(
    settings: Config, i18n: I18n
) -> None:
    """A tool cut by the deadline may have written: the caller tells the
    user so instead of claiming nothing changed."""
    store = Store(release=asyncio.Event())
    async with running_bot(settings, i18n) as running:
        llm = await running.container.get(LLMClient)
        assert isinstance(llm, FakeLLMClient)
        llm.queue_tool_calls(tool_call("store", {"item": "rice"}))
        await _start(running, i18n, [Find(), store], deadline=0.2)
        await running.settle()
        edits = [edit.text for edit in running.session.sent(EditMessageText)]
    assert store.started.is_set()
    assert edits[-1] == "Out of time; part of the change may be saved."
    assert len(llm.calls) == 1
