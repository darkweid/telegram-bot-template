from dataclasses import dataclass

from src.core.i18n import N_


@dataclass(frozen=True, slots=True)
class BotCommandSpec:
    command: str
    description: str  # an N_ msgid, translated per menu language


USER_COMMANDS: tuple[BotCommandSpec, ...] = (
    BotCommandSpec("start", N_("Start the bot")),
    BotCommandSpec("help", N_("Show help")),
    BotCommandSpec("language", N_("Change language")),
)
