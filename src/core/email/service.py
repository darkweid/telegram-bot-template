"""Recipient validation in front of the mailer, and the outbox path to the
worker."""

from collections.abc import Mapping, Sequence
import contextlib
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel, EmailStr, TypeAdapter, ValidationError

from loggers import get_logger
from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.email.disabled import EMAIL_DISABLED_DETAIL
from src.core.email.enums import MessageType
from src.core.email.errors import EmailDisabled
from src.core.email.interface import Mailer
from src.core.email.tasks import SEND_EMAIL, SEND_EMAIL_WITH_S3_ATTACHMENTS
from src.core.storage.s3.disabled import S3_DISABLED_DETAIL
from src.core.storage.s3.errors import S3Disabled

logger = get_logger(__name__)

_EMAIL_ADDRESS = TypeAdapter(EmailStr)


class EmailService:
    """Sends email now (`send_*`) or queues it in the caller's transaction for
    the worker (`enqueue_*`). Invalid addresses are skipped; none valid raises
    ValueError. Queuing refuses while email, or S3 for attachments, is off, so
    no outbox row is written that would fail on every retry.

    Logs carry the template name and counts, never an address, a subject or
    a body.
    """

    def __init__(
        self, mailer: Mailer, *, email_enabled: bool, s3_enabled: bool
    ) -> None:
        self._mailer = mailer
        self._email_enabled = email_enabled
        self._s3_enabled = s3_enabled

    async def send_template_email(
        self,
        subject: str,
        recipients: str | Sequence[str],
        template_name: str,
        template_body: BaseModel | Mapping[str, Any],
        subtype: MessageType = MessageType.HTML,
    ) -> None:
        valid = self._validated_recipients(recipients)
        await self._mailer.send_template(
            subject, valid, template_name, template_body, subtype
        )
        logger.debug("Email %s sent to %d recipient(s)", template_name, len(valid))

    async def send_email_with_attachments(
        self,
        subject: str,
        recipients: str | Sequence[str],
        body_text: str,
        file_paths: list[Path],
        subtype: MessageType = MessageType.PLAIN,
        *,
        cleanup: bool = False,
    ) -> None:
        """With `cleanup` the files are removed whether or not the send went
        through: they are temporary copies made for this email."""
        try:
            valid = self._validated_recipients(recipients)
            await self._mailer.send_with_attachments(
                subject, valid, body_text, file_paths, subtype
            )
        finally:
            if cleanup:
                for file_path in file_paths:
                    with contextlib.suppress(FileNotFoundError, PermissionError):
                        file_path.unlink()
        logger.debug(
            "Email with %d attachment(s) sent to %d recipient(s)",
            len(file_paths),
            len(valid),
        )

    async def enqueue_template_email(
        self,
        uow: SQLAlchemyUnitOfWork,
        subject: str,
        recipients: str | Sequence[str],
        template_name: str,
        template_body: BaseModel | Mapping[str, Any],
        subtype: MessageType = MessageType.HTML,
    ) -> UUID:
        """Queue the email in `uow`'s transaction; the worker sends it after
        commit. Returns the outbox row id."""
        if not self._email_enabled:
            raise EmailDisabled(EMAIL_DISABLED_DETAIL)
        valid = self._validated_recipients(recipients)
        # JSON mode: the outbox stores only what survives a JSON round-trip.
        context = (
            dict(template_body)
            if isinstance(template_body, Mapping)
            else template_body.model_dump(mode="json")
        )
        message_id = await uow.enqueue_task(
            SEND_EMAIL,
            subject=subject,
            recipients=valid,
            template_name=template_name,
            context=context,
            subtype=subtype.value,
        )
        logger.debug("Email %s queued for %d recipient(s)", template_name, len(valid))
        return message_id

    async def enqueue_email_with_s3_attachments(
        self,
        uow: SQLAlchemyUnitOfWork,
        subject: str,
        recipients: str | Sequence[str],
        attachment_keys: Sequence[str],
        *,
        body_text: str = "",
        subtype: MessageType = MessageType.PLAIN,
        cleanup: bool = False,
    ) -> UUID:
        """Queue an email whose attachments the worker downloads from S3; with
        `cleanup` it deletes them once the email went out."""
        if not self._email_enabled:
            raise EmailDisabled(EMAIL_DISABLED_DETAIL)
        if not self._s3_enabled:
            raise S3Disabled(S3_DISABLED_DETAIL)
        if not attachment_keys:
            raise ValueError("At least one attachment key is required.")
        valid = self._validated_recipients(recipients)
        message_id = await uow.enqueue_task(
            SEND_EMAIL_WITH_S3_ATTACHMENTS,
            subject=subject,
            recipients=valid,
            attachment_keys=list(attachment_keys),
            body_text=body_text,
            subtype=subtype.value,
            cleanup=cleanup,
        )
        logger.debug(
            "Email with %d S3 attachment(s) queued for %d recipient(s)",
            len(attachment_keys),
            len(valid),
        )
        return message_id

    def _validated_recipients(self, recipients: str | Sequence[str]) -> list[str]:
        candidates = [recipients] if isinstance(recipients, str) else list(recipients)
        valid: list[str] = []
        for candidate in candidates:
            try:
                valid.append(_EMAIL_ADDRESS.validate_python(candidate))
            except ValidationError:
                # Counted below; the error itself quotes the address.
                continue
        skipped = len(candidates) - len(valid)
        if skipped:
            logger.warning("Skipped %d invalid recipient address(es)", skipped)
        if not valid:
            raise ValueError("No valid recipient emails provided.")
        return valid
