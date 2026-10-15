from pathlib import Path

import pytest

from src.core.database.uow import SQLAlchemyUnitOfWork
from src.core.email.enums import MessageType
from src.core.email.errors import EmailDisabled
from src.core.email.schemas import NOTIFICATION_TEMPLATE, NotificationEmail
from src.core.email.service import EmailService
from src.core.email.tasks import SEND_EMAIL, SEND_EMAIL_WITH_S3_ATTACHMENTS
from src.core.outbox.models import OutboxMessage
from src.core.storage.s3.errors import S3Disabled
from tests.fakes.db import FakeAsyncSession
from tests.fakes.email import FakeMailer
from tests.helpers.logs import captured_logs

ADDRESS = "first@example.com"
BODY = NotificationEmail(title="Weekly report", message="Your report is ready")


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


def build_service(
    mailer: FakeMailer, *, email_enabled: bool = True, s3_enabled: bool = True
) -> EmailService:
    return EmailService(mailer, email_enabled=email_enabled, s3_enabled=s3_enabled)


def outbox_rows(session: FakeAsyncSession) -> list[OutboxMessage]:
    return [
        call.args[0]
        for call in session.add.call_args_list
        if isinstance(call.args[0], OutboxMessage)
    ]


async def test_a_template_email_goes_to_the_mailer(mailer: FakeMailer) -> None:
    await build_service(mailer).send_template_email(
        "Weekly report", [ADDRESS], NOTIFICATION_TEMPLATE, BODY
    )

    [sent] = mailer.sent_template_emails
    assert sent.recipients == [ADDRESS]
    assert sent.template_name == NOTIFICATION_TEMPLATE
    assert sent.template_data["title"] == "Weekly report"
    assert sent.subtype is MessageType.HTML


async def test_a_single_address_may_be_a_string(mailer: FakeMailer) -> None:
    await build_service(mailer).send_template_email(
        "Weekly report", ADDRESS, NOTIFICATION_TEMPLATE, BODY
    )

    assert mailer.sent_template_emails[0].recipients == [ADDRESS]


async def test_invalid_addresses_are_skipped_and_counted_not_named(
    mailer: FakeMailer,
) -> None:
    """An address is personal data, the broken ones included."""
    with captured_logs("src.core.email.service") as records:
        await build_service(mailer).send_template_email(
            "Weekly report",
            ["broken-address", ADDRESS],
            NOTIFICATION_TEMPLATE,
            BODY,
        )

    assert mailer.sent_template_emails[0].recipients == [ADDRESS]
    messages = [record.getMessage() for record in records]
    assert "Skipped 1 invalid recipient address(es)" in messages
    assert not any("broken-address" in message for message in messages)


@pytest.mark.parametrize("recipients", [[], ["broken-address"]])
async def test_no_valid_address_sends_nothing(
    mailer: FakeMailer, recipients: list[str]
) -> None:
    with pytest.raises(ValueError, match="No valid recipient"):
        await build_service(mailer).send_template_email(
            "Weekly report", recipients, NOTIFICATION_TEMPLATE, BODY
        )

    assert mailer.sent_template_emails == []


async def test_logs_carry_neither_addresses_nor_subject_nor_body(
    mailer: FakeMailer,
) -> None:
    """Subjects and bodies are written for one person and often quote what
    they typed; only the template name and counts may reach a log line."""
    with captured_logs("src.core.email.service") as records:
        await build_service(mailer).send_template_email(
            "Secret subject",
            [ADDRESS],
            NOTIFICATION_TEMPLATE,
            NotificationEmail(title="T", message="Secret body"),
        )

    text = " ".join(record.getMessage() for record in records)
    assert NOTIFICATION_TEMPLATE in text
    for private in (ADDRESS, "Secret subject", "Secret body"):
        assert private not in text


async def test_files_are_attached_and_kept_without_cleanup(
    mailer: FakeMailer, tmp_path: Path
) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")

    await build_service(mailer).send_email_with_attachments(
        "Report", [ADDRESS], "See attached", [report]
    )

    [sent] = mailer.sent_attachments
    assert sent.file_paths == [report]
    assert sent.subtype is MessageType.PLAIN
    assert report.exists()


async def test_cleanup_removes_the_files_after_a_send(
    mailer: FakeMailer, tmp_path: Path
) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("a")
    second.write_text("b")

    await build_service(mailer).send_email_with_attachments(
        "Report", [ADDRESS], "See attached", [first, second], cleanup=True
    )

    assert not first.exists()
    assert not second.exists()


async def test_cleanup_removes_the_files_after_a_failure_too(
    mailer: FakeMailer, tmp_path: Path
) -> None:
    """The files are temporary copies made for this email; a failed send must
    not leave them on the disk."""
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    mailer.error = RuntimeError("smtp down")

    with pytest.raises(RuntimeError):
        await build_service(mailer).send_email_with_attachments(
            "Report", [ADDRESS], "See attached", [report], cleanup=True
        )

    assert not report.exists()


async def test_a_failure_without_cleanup_keeps_the_files(
    mailer: FakeMailer, tmp_path: Path
) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    mailer.error = RuntimeError("smtp down")

    with pytest.raises(RuntimeError):
        await build_service(mailer).send_email_with_attachments(
            "Report", [ADDRESS], "See attached", [report]
        )

    assert report.exists()


async def test_a_queued_email_is_an_outbox_row_of_the_transaction(
    mailer: FakeMailer,
) -> None:
    """The worker sends it only if the use case commits, and the row survives
    a Redis outage at commit time."""
    session = FakeAsyncSession()
    async with SQLAlchemyUnitOfWork(session) as uow:
        message_id = await build_service(mailer).enqueue_template_email(
            uow, "Weekly report", ADDRESS, NOTIFICATION_TEMPLATE, BODY
        )
        await uow.commit()

    [row] = outbox_rows(session)
    assert row.id == message_id
    assert row.task_name == SEND_EMAIL
    assert row.args == []
    assert row.kwargs == {
        "subject": "Weekly report",
        "recipients": [ADDRESS],
        "template_name": NOTIFICATION_TEMPLATE,
        "context": {
            "title": "Weekly report",
            "message": "Your report is ready",
            "logo_url": "",
        },
        "subtype": "html",
    }
    assert mailer.sent_template_emails == []


async def test_an_email_with_s3_attachments_is_queued_with_its_keys(
    mailer: FakeMailer,
) -> None:
    session = FakeAsyncSession()
    async with SQLAlchemyUnitOfWork(session) as uow:
        await build_service(mailer).enqueue_email_with_s3_attachments(
            uow,
            "Report",
            [ADDRESS],
            ("mail/42/report.pdf",),
            body_text="See attached",
            cleanup=True,
        )
        await uow.commit()

    [row] = outbox_rows(session)
    assert row.task_name == SEND_EMAIL_WITH_S3_ATTACHMENTS
    assert row.kwargs == {
        "subject": "Report",
        "recipients": [ADDRESS],
        "attachment_keys": ["mail/42/report.pdf"],
        "body_text": "See attached",
        "subtype": "plain",
        "cleanup": True,
    }


async def test_disabled_email_refuses_to_queue_before_any_row(
    mailer: FakeMailer,
) -> None:
    """A queued email with EMAIL_ENABLED=false would fail on every retry in
    the worker; the use case must hear it at once."""
    session = FakeAsyncSession()
    service = build_service(mailer, email_enabled=False)
    async with SQLAlchemyUnitOfWork(session) as uow:
        with pytest.raises(EmailDisabled, match="EMAIL_ENABLED=true"):
            await service.enqueue_template_email(
                uow, "Weekly report", ADDRESS, NOTIFICATION_TEMPLATE, BODY
            )
        with pytest.raises(EmailDisabled):
            await service.enqueue_email_with_s3_attachments(
                uow, "Report", ADDRESS, ["k"]
            )

    assert outbox_rows(session) == []


async def test_disabled_s3_refuses_attachments_before_any_row(
    mailer: FakeMailer,
) -> None:
    session = FakeAsyncSession()
    service = build_service(mailer, s3_enabled=False)
    async with SQLAlchemyUnitOfWork(session) as uow:
        with pytest.raises(S3Disabled, match="S3_ENABLED=true"):
            await service.enqueue_email_with_s3_attachments(
                uow, "Report", ADDRESS, ["k"]
            )

    assert outbox_rows(session) == []


async def test_an_attachment_email_needs_at_least_one_key(
    mailer: FakeMailer,
) -> None:
    session = FakeAsyncSession()
    async with SQLAlchemyUnitOfWork(session) as uow:
        with pytest.raises(ValueError, match="attachment key"):
            await build_service(mailer).enqueue_email_with_s3_attachments(
                uow, "Report", ADDRESS, []
            )

    assert outbox_rows(session) == []
