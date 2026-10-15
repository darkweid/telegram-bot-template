from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiosmtplib
import pytest

from src.core.email import smtp_mailer as smtp_mailer_module
from src.core.email.enums import MessageType
from src.core.email.schemas import NOTIFICATION_TEMPLATE, NotificationEmail
from src.core.email.smtp_mailer import SmtpMailer, SmtpSettings
from src.core.errors.scrubbed import ScrubbedError
from tests.helpers.logs import captured_logs

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def build_settings(**overrides: Any) -> SmtpSettings:
    values: dict[str, Any] = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "sender@example.com",
        "password": "example-password-not-real",
        "from_name": "Example Bot",
        "use_tls": False,
        "start_tls": True,
        "validate_certs": False,
    }
    values.update(overrides)
    return SmtpSettings(**values)


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smtp_mailer_module, "get_utc_now", lambda: NOW)


@pytest.fixture
def send_mock(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    # aiosmtplib.send returns (refused recipients, server reply).
    mock = AsyncMock(return_value=({}, "250 OK"))
    monkeypatch.setattr(smtp_mailer_module.aiosmtplib, "send", mock)
    return mock


def sent_message(send_mock: AsyncMock) -> EmailMessage:
    message: EmailMessage = send_mock.await_args.args[0]
    return message


async def notify(mailer: SmtpMailer, body: NotificationEmail) -> None:
    await mailer.send_template(
        "Report", ["first@example.com"], NOTIFICATION_TEMPLATE, body
    )


async def test_a_template_email_carries_its_headers_and_body(
    send_mock: AsyncMock,
) -> None:
    mailer = SmtpMailer(build_settings())

    await mailer.send_template(
        subject="Weekly report",
        recipients=["first@example.com", "second@example.com"],
        template_name=NOTIFICATION_TEMPLATE,
        template_data={"title": "Notification", "message": "Hello"},
    )

    message = sent_message(send_mock)
    assert message["Subject"] == "Weekly report"
    assert message["From"] == "Example Bot <sender@example.com>"
    assert message["To"] == "first@example.com, second@example.com"
    assert message["Date"] == format_datetime(NOW)
    assert message["Message-ID"]
    assert message.get_content_type() == "text/html"
    assert "Notification" in message.get_content()


async def test_a_pydantic_body_renders_like_a_dict(send_mock: AsyncMock) -> None:
    await notify(
        SmtpMailer(build_settings()),
        NotificationEmail(title="Weekly report", message="Ready"),
    )

    body = sent_message(send_mock).get_content()
    assert "Weekly report" in body
    assert "Ready" in body


async def test_a_plain_subtype_is_sent_as_text(send_mock: AsyncMock) -> None:
    await SmtpMailer(build_settings()).send_template(
        "Plain",
        ["first@example.com"],
        NOTIFICATION_TEMPLATE,
        {"title": "Notification", "message": "Hello"},
        subtype=MessageType.PLAIN,
    )

    assert sent_message(send_mock).get_content_type() == "text/plain"


async def test_the_message_is_escaped_and_keeps_its_line_breaks(
    send_mock: AsyncMock,
) -> None:
    """The message is often text a user typed; unescaped, it could inject
    markup and links into an email sent under the bot's name."""
    await notify(
        SmtpMailer(build_settings()),
        NotificationEmail(title="T", message="line one\n<a href='x'>click</a>"),
    )

    body = sent_message(send_mock).get_content()
    assert "line one<br>&lt;a href=&#39;x&#39;&gt;click&lt;/a&gt;" in body
    assert "<a href='x'>" not in body


async def test_the_logo_row_appears_only_with_a_logo(send_mock: AsyncMock) -> None:
    """An empty src renders a broken image at the top of every email."""
    mailer = SmtpMailer(build_settings())

    await notify(mailer, NotificationEmail(title="T", message="M"))
    assert "<img" not in sent_message(send_mock).get_content()

    await notify(
        mailer,
        NotificationEmail(
            title="T", message="M", logo_url="https://example.com/logo.png"
        ),
    )
    assert 'src="https://example.com/logo.png"' in sent_message(send_mock).get_content()


async def test_the_footer_names_the_sender_and_the_year(send_mock: AsyncMock) -> None:
    await notify(
        SmtpMailer(build_settings()), NotificationEmail(title="T", message="M")
    )

    body = sent_message(send_mock).get_content()
    assert "Copyright © 2026 Example Bot. All rights reserved." in body
    assert "TemplateReference" not in body


async def test_files_are_attached_with_their_types(
    send_mock: AsyncMock, tmp_path: Path
) -> None:
    text_file = tmp_path / "report.txt"
    text_file.write_text("report body")
    binary_file = tmp_path / "logo.png"
    binary_file.write_bytes(b"\x89PNG\r\n")

    await SmtpMailer(build_settings()).send_with_attachments(
        "Report", ["first@example.com"], "See attached", [text_file, binary_file]
    )

    attachments = list(sent_message(send_mock).iter_attachments())
    assert [part.get_filename() for part in attachments] == ["report.txt", "logo.png"]
    assert attachments[0].get_content_type() == "text/plain"
    assert attachments[1].get_content_type() == "image/png"
    assert attachments[1].get_payload(decode=True) == b"\x89PNG\r\n"


async def test_bytes_are_attached_with_their_types(send_mock: AsyncMock) -> None:
    await SmtpMailer(build_settings()).send_with_attachment_bytes(
        "Report",
        ["first@example.com"],
        "See attached",
        [("report.txt", b"report body"), ("data.unknownext", b"\x00\x01")],
    )

    attachments = list(sent_message(send_mock).iter_attachments())
    assert [part.get_filename() for part in attachments] == [
        "report.txt",
        "data.unknownext",
    ]
    assert attachments[0].get_content_type() == "text/plain"
    assert attachments[1].get_content_type() == "application/octet-stream"


async def test_the_transport_gets_every_setting(send_mock: AsyncMock) -> None:
    mailer = SmtpMailer(build_settings(port=465, use_tls=True, start_tls=False))

    await mailer.send_with_attachments("Report", ["first@example.com"], "Hi", [])

    assert send_mock.await_args.kwargs == {
        "hostname": "smtp.example.com",
        "port": 465,
        "username": "sender@example.com",
        "password": "example-password-not-real",
        "use_tls": True,
        "start_tls": False,
        "validate_certs": False,
    }


async def test_no_password_means_no_authentication(send_mock: AsyncMock) -> None:
    """A local catcher takes mail without AUTH; blank credentials would fail
    the handshake instead of skipping it."""
    mailer = SmtpMailer(build_settings(password=""))

    await mailer.send_with_attachments("Report", ["first@example.com"], "Hi", [])

    assert send_mock.await_args.kwargs["username"] is None
    assert send_mock.await_args.kwargs["password"] is None


async def test_a_refused_recipient_never_reaches_the_error(
    send_mock: AsyncMock,
) -> None:
    """taskiq logs a failed task with its exception, and Sentry records it;
    aiosmtplib puts the refused address into the exception text."""
    send_mock.side_effect = aiosmtplib.SMTPRecipientsRefused(
        [
            aiosmtplib.SMTPRecipientRefused(
                550, "5.1.1 <first@example.com> rejected", "first@example.com"
            )
        ]
    )

    with pytest.raises(ScrubbedError) as raised:
        await notify(
            SmtpMailer(build_settings()), NotificationEmail(title="T", message="M")
        )

    assert str(raised.value) == (
        "SMTP delivery failed: SMTPRecipientsRefused, 1 recipient(s) refused"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_a_refused_sender_keeps_only_its_reply_code(send_mock: AsyncMock) -> None:
    send_mock.side_effect = aiosmtplib.SMTPSenderRefused(
        553, "<sender@example.com> not allowed", "sender@example.com"
    )

    with pytest.raises(ScrubbedError) as raised:
        await notify(
            SmtpMailer(build_settings()), NotificationEmail(title="T", message="M")
        )

    assert str(raised.value) == "SMTP delivery failed: SMTPSenderRefused (code 553)"


@pytest.mark.parametrize(
    "error",
    [
        aiosmtplib.SMTPServerDisconnected("Connection lost to mail.example.com"),
        aiosmtplib.SMTPConnectError("Could not connect to mail.example.com:587"),
    ],
    ids=lambda error: type(error).__name__,
)
async def test_a_transport_failure_names_only_its_class(
    send_mock: AsyncMock, error: aiosmtplib.SMTPException
) -> None:
    """Connection errors carry the host in their text and no reply code; the
    summary must not invent a code and must not keep the original as a chain."""
    send_mock.side_effect = error

    with pytest.raises(ScrubbedError) as raised:
        await notify(
            SmtpMailer(build_settings()), NotificationEmail(title="T", message="M")
        )

    assert str(raised.value) == f"SMTP delivery failed: {type(error).__name__}"
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


async def test_a_partial_refusal_is_logged_as_a_count(send_mock: AsyncMock) -> None:
    """The server took the email for the other recipients, so nothing raises;
    the log line must still not name who was refused."""
    send_mock.return_value = (
        {"second@example.com": aiosmtplib.SMTPResponse(550, "no such user")},
        "250 OK",
    )

    with captured_logs("src.core.email.smtp_mailer") as records:
        await SmtpMailer(build_settings()).send_template(
            "Report",
            ["first@example.com", "second@example.com"],
            NOTIFICATION_TEMPLATE,
            {"title": "T", "message": "M"},
        )

    assert [record.getMessage() for record in records] == [
        "SMTP server refused 1 of 2 recipient(s)"
    ]


def test_the_smtp_password_stays_out_of_the_settings_repr() -> None:
    assert "example-password-not-real" not in repr(build_settings())
