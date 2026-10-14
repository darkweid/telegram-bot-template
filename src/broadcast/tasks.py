from uuid import UUID

from dishka import FromDishka

from src.broadcast.task_names import RESUME_STALLED_BROADCASTS, RUN_BROADCAST
from src.broadcast.usecases.resume_broadcasts import ResumeStalledBroadcastsUseCase
from src.broadcast.usecases.run_broadcast import RunBroadcastUseCase
from src.core.tasks.registry import TaskSpec


async def run_broadcast(broadcast_id: str, run: FromDishka[RunBroadcastUseCase]) -> str:
    # The id travels as a string: task arguments must survive a JSON round trip.
    status = await run.execute(UUID(broadcast_id))
    return f"Broadcast {broadcast_id}: {status or 'owned by another run'}"


async def resume_stalled_broadcasts(
    resume: FromDishka[ResumeStalledBroadcastsUseCase],
) -> str:
    resumed = await resume.execute()
    return f"Resumed {resumed} broadcasts."


# No retry for either: a failed broadcast is marked FAILED and must stay so,
# and a run that was cut off comes back through the resume task.
BROADCAST_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(RUN_BROADCAST, run_broadcast),
    TaskSpec(
        RESUME_STALLED_BROADCASTS,
        resume_stalled_broadcasts,
        schedule=({"cron": "* * * * *"},),
    ),
)
