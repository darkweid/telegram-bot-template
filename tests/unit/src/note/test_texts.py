from uuid import UUID

from aiogram.utils.i18n import I18n

from src.note.schemas import NotePage, NoteView
from src.note.texts import PREVIEW_LENGTH, delete_prompt, page_text, preview

NOTE_ID = UUID("01900000-0000-7000-8000-000000000001")


def test_a_short_note_previews_as_is() -> None:
    assert preview("Buy milk") == "Buy milk"


def test_a_multiline_note_previews_on_one_line() -> None:
    """A list line with a line break inside would read as two notes."""
    assert preview("Buy\n\nmilk  and bread") == "Buy milk and bread"


def test_a_long_note_is_cut_with_an_ellipsis() -> None:
    cut = preview("word " * 50)
    assert len(cut) == PREVIEW_LENGTH
    assert cut.endswith("…")


def test_the_page_escapes_what_the_user_wrote(i18n: I18n) -> None:
    """Replies use HTML parse mode: an unescaped `<b>` would be rendered, and
    an unclosed `<` would make Telegram refuse the whole message."""
    page = NotePage(
        notes=(NoteView(id=NOTE_ID, text="<b>bold</b> & co"),),
        has_newer=False,
        has_older=False,
    )
    with i18n.context(), i18n.use_locale("en"):
        text = page_text(page)
    assert text == "Your notes:\n\n1. &lt;b&gt;bold&lt;/b&gt; &amp; co"


def test_an_empty_page_invites_a_first_note(i18n: I18n) -> None:
    with i18n.context(), i18n.use_locale("ru"):
        text = page_text(NotePage(notes=(), has_newer=False, has_older=False))
    assert text == "Заметок пока нет. Напишите первую: /note."


def test_the_delete_prompt_shows_the_whole_note_escaped(i18n: I18n) -> None:
    with i18n.context(), i18n.use_locale("en"):
        text = delete_prompt(NoteView(id=NOTE_ID, text="a < b\nsecond line"))
    assert text == "Delete this note?\n\na &lt; b\nsecond line"
