from aiogram import html
from aiogram.utils.i18n import gettext as _

from src.note.schemas import NotePage, NoteView

# A list line stays on one line of a phone screen.
PREVIEW_LENGTH = 60


def preview(text: str) -> str:
    """The note on one line, cut to PREVIEW_LENGTH characters."""
    line = " ".join(text.split())
    if len(line) <= PREVIEW_LENGTH:
        return line
    return line[: PREVIEW_LENGTH - 1].rstrip() + "…"


def page_text(page: NotePage) -> str:
    if not page.notes:
        return _("You have no notes yet. Send /note to write one.")
    lines = [_("Your notes:"), ""]
    lines += [
        f"{number}. {html.quote(preview(note.text))}"
        for number, note in enumerate(page.notes, start=1)
    ]
    return "\n".join(lines)


def delete_prompt(note: NoteView) -> str:
    return _("Delete this note?\n\n{text}").format(text=html.quote(note.text))
