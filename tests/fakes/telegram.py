from collections import defaultdict, deque
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import UnionType
from typing import Any, TypeVar, get_args

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import GetMe, TelegramMethod
from aiogram.types import Chat, Message, User

TEST_BOT_TOKEN = "123456789:AAtest-bot-token-not-real-0000000000000"
BOT_ID = 123456789

M = TypeVar("M", bound=TelegramMethod[Any])


def bot_user() -> User:
    return User(id=BOT_ID, is_bot=True, first_name="Test bot", username="test_bot")


def _chat_type(chat_id: int) -> str:
    """Telegram's id ranges: -100... supergroups and channels, other negatives groups."""
    if chat_id < 0:
        return "supergroup" if str(chat_id).startswith("-100") else "group"
    return "private"


def _returns(method: TelegramMethod[Any]) -> tuple[Any, ...]:
    returning = method.__returning__
    return get_args(returning) if isinstance(returning, UnionType) else (returning,)


class FakeTelegramSession(BaseSession):
    """A Bot API session that records calls instead of sending them.

    Results are queued per method type; without one, a call that returns a
    bool succeeds and a call that returns a Message gets one echoing its chat
    and text, bound to the bot like a real reply, which covers what handlers
    send. Anything else must be queued, so a test never passes on a made-up
    answer it did not ask for. Queued results come back exactly as given:
    bind a queued Message with `.as_(bot)` if the code under test calls its
    methods.
    """

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self._queued: defaultdict[type[TelegramMethod[Any]], deque[object]] = (
            defaultdict(deque)
        )
        self._message_ids = iter(range(1, 1_000_000))

    def queue(self, method_type: type[TelegramMethod[Any]], result: object) -> None:
        if not (
            isinstance(method_type, type) and issubclass(method_type, TelegramMethod)
        ):
            raise TypeError(f"Expected a TelegramMethod subclass, got {method_type!r}")
        self._queued[method_type].append(result)

    def sent(self, method_type: type[M]) -> list[M]:
        return [
            request for request in self.requests if isinstance(request, method_type)
        ]

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[Any],
        timeout: int | None = None,  # noqa: ASYNC109 - signature fixed by BaseSession
    ) -> Any:
        self.requests.append(method)
        queued = self._queued[type(method)]
        if queued:
            result = queued.popleft()
            if isinstance(result, BaseException):
                raise result
            return result
        return self._default_result(bot, method)

    def _default_result(self, bot: Bot, method: TelegramMethod[Any]) -> Any:
        if isinstance(method, GetMe):
            return bot_user()
        returns = _returns(method)
        if Message in returns:
            chat_id = getattr(method, "chat_id", None)
            if chat_id is None and bool in returns:
                # An edit addressed by inline_message_id: the real API answers True.
                return True
            if not isinstance(chat_id, int):
                raise LookupError(
                    f"Queue a result for {type(method).__name__}: "
                    "the fake only echoes messages sent to a numeric chat id"
                )
            return Message(
                message_id=next(self._message_ids),
                date=datetime.now(UTC),
                chat=Chat(id=chat_id, type=_chat_type(chat_id)),
                from_user=bot_user(),
                text=getattr(method, "text", None),
            ).as_(bot)
        if returns == (bool,):
            return True
        raise LookupError(f"Queue a result for {type(method).__name__} first")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,  # noqa: ASYNC109 - signature fixed by BaseSession
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes]:
        raise LookupError("The fake session does not download files")
        yield b""  # pragma: no cover - makes this an async generator

    async def close(self) -> None:
        return None


def make_bot(session: FakeTelegramSession) -> Bot:
    return Bot(TEST_BOT_TOKEN, session=session)
