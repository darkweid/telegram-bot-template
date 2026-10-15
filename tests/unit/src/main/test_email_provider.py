from pathlib import Path
from unittest.mock import AsyncMock

from pydantic import SecretStr
import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.email import smtp_mailer as smtp_mailer_module
from src.core.email.disabled import DisabledMailer
from src.core.email.errors import EmailDisabled
from src.core.email.interface import Mailer
from src.core.email.schemas import NOTIFICATION_TEMPLATE, NotificationEmail
from src.core.email.service import EmailService
from src.core.email.smtp_mailer import SmtpMailer
from src.core.storage.s3.errors import S3Disabled
from src.main.config import Config, EmailConfig
from src.main.di import build_container
from tests.fakes.db import FakeAsyncSession

ADDRESS = "first@example.com"


def enabled_email() -> EmailConfig:
    return EmailConfig(
        EMAIL_ENABLED=True,
        EMAIL_SERVER="smtp.example.com",
        EMAIL_PORT=465,
        EMAIL_USER="bot@example.com",
        EMAIL_PASSWORD=SecretStr("example-password-not-real"),
        EMAIL_FROM_NAME="Example Bot",
        EMAIL_USE_TLS=True,
        EMAIL_STARTTLS=False,
        EMAIL_VALIDATE_CERTS=False,
    )


async def test_with_email_off_every_send_names_the_settings(settings: Config) -> None:
    """STRICT_VALIDATION builds the container without any EMAIL_* key, and a
    send explains itself instead of failing to connect to an empty host."""
    container = build_container(settings)
    try:
        mailer = await container.get(Mailer)
        assert isinstance(mailer, DisabledMailer)
        sends = [
            mailer.send_template("s", [ADDRESS], NOTIFICATION_TEMPLATE, {}),
            mailer.send_with_attachments("s", [ADDRESS], "b", [Path("a.txt")]),
            mailer.send_with_attachment_bytes("s", [ADDRESS], "b", [("a", b"x")]),
        ]
        for send in sends:
            with pytest.raises(EmailDisabled, match="EMAIL_ENABLED=true"):
                await send
    finally:
        await container.close()


async def test_the_enabled_mailer_gets_every_setting(
    settings: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A setting dropped on the way to aiosmtplib is a silent default in prod:
    plain text where TLS was asked for, or a skipped certificate check."""
    send = AsyncMock(return_value=({}, "250 OK"))
    monkeypatch.setattr(smtp_mailer_module.aiosmtplib, "send", send)
    container = build_container(settings.model_copy(update={"email": enabled_email()}))
    try:
        mailer = await container.get(Mailer)
        assert isinstance(mailer, SmtpMailer)
        await mailer.send_template(
            "Report",
            [ADDRESS],
            NOTIFICATION_TEMPLATE,
            NotificationEmail(title="T", message="M"),
        )
    finally:
        await container.close()

    assert send.await_args.kwargs == {
        "hostname": "smtp.example.com",
        "port": 465,
        "username": "bot@example.com",
        "password": "example-password-not-real",
        "use_tls": True,
        "start_tls": False,
        "validate_certs": False,
    }
    assert send.await_args.args[0]["From"] == "Example Bot <bot@example.com>"


@pytest.mark.parametrize(
    ("email_on", "error"), [(False, EmailDisabled), (True, S3Disabled)]
)
async def test_the_service_knows_which_backend_is_off(
    settings: Config, email_on: bool, error: type[Exception]
) -> None:
    """The flags reach EmailService from the config sections, not from the
    mailer, so a queue request is refused before any outbox row."""
    config = settings
    if email_on:
        config = settings.model_copy(update={"email": enabled_email()})
    container = build_container(config)
    try:
        service = await container.get(EmailService)
        async with SQLAlchemyUnitOfWork(FakeAsyncSession()) as uow:
            with pytest.raises(error):
                await service.enqueue_email_with_s3_attachments(
                    uow, "Report", ADDRESS, ["k"]
                )
    finally:
        await container.close()
