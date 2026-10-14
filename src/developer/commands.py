from src.core.telegram.commands import BotCommandSpec

# Shown in the menu of every id in DEVELOPER_IDS, after the user commands
# (and the admin ones for someone holding both roles).
DEVELOPER_COMMANDS: tuple[BotCommandSpec, ...] = ()
