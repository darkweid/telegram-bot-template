from src.core.i18n import N_
from src.core.telegram.commands import BotCommandSpec

USER_COMMANDS: tuple[BotCommandSpec, ...] = (
    BotCommandSpec("start", N_("Start the bot")),
    BotCommandSpec("help", N_("Show help")),
    BotCommandSpec("language", N_("Change language")),
)
