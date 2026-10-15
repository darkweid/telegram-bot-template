from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.core.email.enums import MessageType


@dataclass(frozen=True, slots=True)
class SentTemplateEmail:
    subject: str
    recipients: list[str]
    template_name: str
    template_data: dict[str, Any]
    subtype: MessageType


@dataclass(frozen=True, slots=True)
class SentFileEmail:
    subject: str
    recipients: list[str]
    body_text: str
    file_paths: list[Path]
    subtype: MessageType


@dataclass(frozen=True, slots=True)
class SentBytesEmail:
    subject: str
    recipients: list[str]
    body_text: str
    attachments: list[tuple[str, bytes]]
    subtype: MessageType


class FakeMailer:
    """Records every send. With `error` set, every send raises it and records
    nothing, as a failed SMTP delivery would."""

    def __init__(self) -> None:
        self.sent_template_emails: list[SentTemplateEmail] = []
        self.sent_attachments: list[SentFileEmail] = []
        self.sent_attachment_bytes: list[SentBytesEmail] = []
        self.error: Exception | None = None

    async def send_template(
        self,
        subject: str,
        recipients: list[str],
        template_name: str,
        template_data: BaseModel | Mapping[str, Any],
        subtype: MessageType = MessageType.HTML,
    ) -> None:
        if self.error is not None:
            raise self.error
        payload = (
            dict(template_data)
            if isinstance(template_data, Mapping)
            else template_data.model_dump()
        )
        self.sent_template_emails.append(
            SentTemplateEmail(subject, recipients, template_name, payload, subtype)
        )

    async def send_with_attachments(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        file_paths: list[Path],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.sent_attachments.append(
            SentFileEmail(subject, recipients, body_text, file_paths, subtype)
        )

    async def send_with_attachment_bytes(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        attachments: list[tuple[str, bytes]],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.sent_attachment_bytes.append(
            SentBytesEmail(subject, recipients, body_text, attachments, subtype)
        )
