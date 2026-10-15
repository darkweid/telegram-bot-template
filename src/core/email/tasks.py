"""The worker side of email: what EmailService queues runs here.

Logs carry the template name and counts only. A recipient, a subject or a
body is personal data, and taskiq logs a failed task's arguments at DEBUG.
"""

from pathlib import PurePosixPath
from typing import Any

from dishka.integrations.taskiq import FromDishka

from loggers import get_logger
from src.core.email.enums import MessageType
from src.core.email.interface import Mailer
from src.core.storage.s3.interface import S3Client
from src.core.tasks.registry import TaskSpec

logger = get_logger(__name__)

SEND_EMAIL = "email.send"
SEND_EMAIL_WITH_S3_ATTACHMENTS = "email.send_with_s3_attachments"


async def send_email_task(
    subject: str,
    recipients: list[str],
    template_name: str,
    context: dict[str, Any],
    subtype: str = MessageType.HTML.value,
    *,
    mailer: FromDishka[Mailer],
) -> None:
    await mailer.send_template(
        subject, recipients, template_name, context, MessageType(subtype)
    )
    logger.info("Email %s sent to %d recipient(s)", template_name, len(recipients))


async def send_email_with_s3_attachments_task(
    subject: str,
    recipients: list[str],
    attachment_keys: list[str],
    body_text: str = "",
    subtype: str = MessageType.PLAIN.value,
    cleanup: bool = False,
    *,
    mailer: FromDishka[Mailer],
    s3: FromDishka[S3Client],
) -> None:
    """Each key is attached under its last path segment. With `cleanup` the
    keys are deleted only after the send went through, so a retry can still
    download them."""
    attachments = [
        (PurePosixPath(key).name, await s3.download_bytes(key))
        for key in attachment_keys
    ]
    await mailer.send_with_attachment_bytes(
        subject, recipients, body_text, attachments, MessageType(subtype)
    )
    logger.info(
        "Email with %d attachment(s) sent to %d recipient(s)",
        len(attachments),
        len(recipients),
    )
    if cleanup:
        await _delete_attachments(s3, attachment_keys)


async def _delete_attachments(s3: S3Client, keys: list[str]) -> None:
    failed = 0
    for key in keys:
        # The email is already out: a failure raised here would make
        # SmartRetryMiddleware rerun the task and send it again.
        try:
            await s3.delete_object(key)
        except Exception:
            failed += 1
    if failed:
        logger.warning(
            "Could not delete %d of %d attachment(s) after the email went out",
            failed,
            len(keys),
        )


EMAIL_TASKS: tuple[TaskSpec, ...] = (
    # Retried: an SMTP server or S3 down for a minute is the usual failure.
    TaskSpec(SEND_EMAIL, send_email_task, retry_on_error=True),
    TaskSpec(
        SEND_EMAIL_WITH_S3_ATTACHMENTS,
        send_email_with_s3_attachments_task,
        retry_on_error=True,
    ),
)
