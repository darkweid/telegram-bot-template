from src.core.i18n import N_
from src.core.telegram.commands import BotCommandSpec

NOTE_COMMANDS: tuple[BotCommandSpec, ...] = (
    BotCommandSpec("note", N_("Write a note")),
    BotCommandSpec("notes", N_("Show your notes")),
)
