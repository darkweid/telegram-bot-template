from dishka import FromDishka

from src.core.tasks.registry import TaskSpec
from src.user.usecases.notify_admins import NotifyAdminsOfNewUserUseCase

NOTIFY_ADMINS_OF_NEW_USER = "user.notify_admins_of_new_user"


async def notify_admins_of_new_user(
    user_id: int, notify: FromDishka[NotifyAdminsOfNewUserUseCase]
) -> str:
    sent = await notify.execute(user_id)
    return f"Notified {sent} admins."


# No retry: a retry would repeat the notice to every admin who already got it.
USER_TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(NOTIFY_ADMINS_OF_NEW_USER, notify_admins_of_new_user),
)
