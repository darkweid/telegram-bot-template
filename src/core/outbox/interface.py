from typing import Any, Protocol
from uuid import UUID


class TaskPublisher(Protocol):
    """Sends a stored outbox row to the task broker once its transaction has
    committed. Best-effort: when it fails, the sweeper sends the row later."""

    async def publish(
        self, message_id: UUID, task_name: str, args: list[Any], kwargs: dict[str, Any]
    ) -> None: ...
