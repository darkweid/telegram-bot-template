"""The mailer of a bot that sends no email."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.core.email.enums import MessageType
from src.core.email.errors import EmailDisabled
from src.core.email.interface import Mailer

EMAIL_DISABLED_DETAIL = (
    "Email is disabled: set EMAIL_ENABLED=true and EMAIL_SERVER, EMAIL_USER, "
    "EMAIL_FROM_NAME"
)


class DisabledMailer(Mailer):
    """Stands in for SmtpMailer while EMAIL_ENABLED is false: the container
    still builds, and the first send says which settings are missing."""

    async def send_template(
        self,
        subject: str,
        recipients: list[str],
        template_name: str,
        template_data: BaseModel | Mapping[str, Any],
        subtype: MessageType = MessageType.HTML,
    ) -> None:
        raise EmailDisabled(EMAIL_DISABLED_DETAIL)

    async def send_with_attachments(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        file_paths: list[Path],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        raise EmailDisabled(EMAIL_DISABLED_DETAIL)

    async def send_with_attachment_bytes(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        attachments: list[tuple[str, bytes]],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        raise EmailDisabled(EMAIL_DISABLED_DETAIL)
