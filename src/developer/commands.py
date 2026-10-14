from src.core.i18n import N_
from src.core.telegram.commands import BotCommandSpec

# Shown in the menu of every id in DEVELOPER_IDS, after the user commands
# (and the admin ones for someone holding both roles).
DEVELOPER_COMMANDS: tuple[BotCommandSpec, ...] = (
    BotCommandSpec("status", N_("Show the bot's status")),
    BotCommandSpec("test_alert", N_("Send a test alert")),
)
