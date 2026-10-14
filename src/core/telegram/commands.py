from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BotCommandSpec:
    command: str
    description: str  # an N_ msgid, translated per menu language
