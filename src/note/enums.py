from enum import StrEnum


class PageDirection(StrEnum):
    """Which way a page of notes is read from its cursor: OLDER past the
    last note shown, NEWER before the first one."""

    OLDER = "older"
    NEWER = "newer"


class DeleteAction(StrEnum):
    ASK = "ask"
    CONFIRM = "confirm"
