from src.core.i18n import N_
from src.core.telegram.commands import BotCommandSpec

# Shown in the menu of every id in ADMIN_IDS, after the user commands.
ADMIN_COMMANDS: tuple[BotCommandSpec, ...] = (
    BotCommandSpec("ban", N_("Ban a user")),
    BotCommandSpec("unban", N_("Unban a user")),
)
