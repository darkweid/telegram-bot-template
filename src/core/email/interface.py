from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from src.core.email.enums import MessageType


class Mailer(Protocol):
    async def send_template(
        self,
        subject: str,
        recipients: list[str],
        template_name: str,
        template_data: BaseModel | Mapping[str, Any],
        subtype: MessageType = MessageType.HTML,
    ) -> None:
        """Render `template_name` with `template_data` and send it."""

    async def send_with_attachments(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        file_paths: list[Path],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        """Send `body_text` with every file attached, read from disk."""

    async def send_with_attachment_bytes(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        attachments: list[tuple[str, bytes]],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        """Send `body_text` with attachments held in memory as (filename, data)."""
