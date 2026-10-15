"""SMTP delivery of rendered templates and attachments."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid
import mimetypes
from pathlib import Path
from typing import Any

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from loggers import get_logger
from src.core.email.enums import MessageType
from src.core.email.interface import Mailer
from src.core.errors.scrubbed import scrubbed
from src.core.utils.datetime_utils import get_utc_now

logger = get_logger(__name__)

TEMPLATES_DIRECTORY = Path(__file__).parent / "templates"
DEFAULT_ATTACHMENT_MIME_TYPE = "application/octet-stream"


def build_template_environment(directory: Path = TEMPLATES_DIRECTORY) -> Environment:
    """The Jinja2 environment for email bodies; HTML is autoescaped."""
    return Environment(
        loader=FileSystemLoader(directory),
        autoescape=select_autoescape(["html", "htm", "xml"]),
    )


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    from_name: str
    use_tls: bool
    start_tls: bool
    validate_certs: bool


def _describe_smtp_error(error: aiosmtplib.SMTPException) -> str:
    """Class name, reply code and refusal count only: the reply text and the
    exception's fields quote the addresses the server refused."""
    summary = f"SMTP delivery failed: {type(error).__name__}"
    if isinstance(error, aiosmtplib.SMTPResponseException):
        summary += f" (code {error.code})"
    if isinstance(error, aiosmtplib.SMTPRecipientsRefused):
        summary += f", {len(error.recipients)} recipient(s) refused"
    return summary


class SmtpMailer(Mailer):
    """Renders Jinja2 templates and delivers them over SMTP with aiosmtplib.

    Every template gets `year` and `sender_name` (EMAIL_FROM_NAME) besides
    its own context. An SMTP failure leaves as a ScrubbedError without the
    addresses; a partial refusal is logged as a count.
    """

    def __init__(
        self,
        settings: SmtpSettings,
        template_environment: Environment | None = None,
    ) -> None:
        self._settings = settings
        self._templates = template_environment or build_template_environment()

    async def send_template(
        self,
        subject: str,
        recipients: list[str],
        template_name: str,
        template_data: BaseModel | Mapping[str, Any],
        subtype: MessageType = MessageType.HTML,
    ) -> None:
        context = (
            template_data
            if isinstance(template_data, Mapping)
            else template_data.model_dump()
        )
        template = self._templates.get_template(template_name)
        body = template.render(
            {
                "year": get_utc_now().year,
                "sender_name": self._settings.from_name,
                **context,
            }
        )
        message = self._build_message(subject, recipients)
        message.set_content(body, subtype=subtype.value)
        await self._send(message, len(recipients))

    async def send_with_attachments(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        file_paths: list[Path],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        message = self._build_message(subject, recipients)
        message.set_content(body_text, subtype=subtype.value)
        for file_path in file_paths:
            # In a thread: a large file read would otherwise block the loop.
            payload = await asyncio.to_thread(file_path.read_bytes)
            self._attach_bytes(message, file_path.name, payload)
        await self._send(message, len(recipients))

    async def send_with_attachment_bytes(
        self,
        subject: str,
        recipients: list[str],
        body_text: str,
        attachments: list[tuple[str, bytes]],
        subtype: MessageType = MessageType.PLAIN,
    ) -> None:
        message = self._build_message(subject, recipients)
        message.set_content(body_text, subtype=subtype.value)
        for filename, data in attachments:
            self._attach_bytes(message, filename, data)
        await self._send(message, len(recipients))

    def _build_message(self, subject: str, recipients: list[str]) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = formataddr(
            (self._settings.from_name, self._settings.username)
        )
        message["To"] = ", ".join(recipients)
        message["Date"] = format_datetime(get_utc_now())
        message["Message-ID"] = make_msgid()
        return message

    def _attach_bytes(self, message: EmailMessage, filename: str, data: bytes) -> None:
        guessed_type, _ = mimetypes.guess_type(filename)
        maintype, _, subtype = (guessed_type or DEFAULT_ATTACHMENT_MIME_TYPE).partition(
            "/"
        )
        message.add_attachment(
            data, maintype=maintype, subtype=subtype, filename=filename
        )

    async def _send(self, message: EmailMessage, recipient_count: int) -> None:
        settings = self._settings
        # An empty password means the relay takes no authentication (a local
        # catcher); blank credentials would fail the AUTH handshake instead.
        authenticated = bool(settings.username and settings.password)
        try:
            refused, _ = await aiosmtplib.send(
                message,
                hostname=settings.host,
                port=settings.port,
                username=settings.username if authenticated else None,
                password=settings.password if authenticated else None,
                use_tls=settings.use_tls,
                start_tls=settings.start_tls,
                validate_certs=settings.validate_certs,
            )
        except aiosmtplib.SMTPException as error:
            failure = scrubbed(_describe_smtp_error(error), error)
        else:
            if refused:
                logger.warning(
                    "SMTP server refused %d of %d recipient(s)",
                    len(refused),
                    recipient_count,
                )
            return
        # Raised outside the handler: inside it the original error, whose text
        # quotes the refused addresses, would ride along as __context__.
        raise failure
