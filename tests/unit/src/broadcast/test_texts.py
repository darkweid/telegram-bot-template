from uuid import UUID

from aiogram.utils.i18n import I18n

from src.broadcast.enums import BroadcastStatus
from src.broadcast.schemas import BroadcastProgress
from src.broadcast.texts import TITLES, preview_text, progress_text

BROADCAST_ID = UUID("01900000-0000-7000-8000-000000000001")


def test_every_status_has_a_title() -> None:
    """A status without one would crash the report of that very outcome."""
    assert set(TITLES) == set(BroadcastStatus)


def test_the_report_counts_every_outcome(i18n: I18n) -> None:
    progress = BroadcastProgress(
        BROADCAST_ID,
        BroadcastStatus.COMPLETED,
        total=4,
        sent=2,
        failed=1,
        unreachable=1,
    )
    with i18n.context(), i18n.use_locale("ru"):
        text = progress_text(progress)
    assert text == "Рассылка завершена: 4 из 4\nДоставлено: 2\nНедоступны: 1\nОшибки: 1"


def test_the_preview_count_is_pluralised(i18n: I18n) -> None:
    with i18n.context(), i18n.use_locale("ru"):
        texts = [preview_text(count) for count in (1, 3, 5)]
    assert texts == [
        "Это сообщение получит 1 пользователь. Отправить?",
        "Это сообщение получат 3 пользователя. Отправить?",
        "Это сообщение получат 5 пользователей. Отправить?",
    ]
