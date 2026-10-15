from unittest.mock import AsyncMock

import pytest
from taskiq import InMemoryBroker

from src.core.email.enums import MessageType
from src.core.email.schemas import NOTIFICATION_TEMPLATE
from src.core.email.tasks import (
    EMAIL_TASKS,
    SEND_EMAIL,
    SEND_EMAIL_WITH_S3_ATTACHMENTS,
    send_email_task,
    send_email_with_s3_attachments_task,
)
from src.core.storage.s3.disabled import DisabledS3Client
from src.core.storage.s3.errors import S3Disabled
from src.core.tasks.registry import register_tasks
from tests.fakes.email import FakeMailer
from tests.fakes.s3 import InMemoryS3Client
from tests.helpers.logs import captured_logs

ADDRESS = "first@example.com"
KEY = "mail/42/report.pdf"


@pytest.fixture
async def s3() -> InMemoryS3Client:
    client = InMemoryS3Client()
    await client.upload_bytes(KEY, b"%PDF")
    return client


async def test_the_send_task_renders_through_the_mailer() -> None:
    mailer = FakeMailer()

    with captured_logs("src.core.email.tasks") as records:
        await send_email_task(
            "Weekly report",
            [ADDRESS],
            NOTIFICATION_TEMPLATE,
            {"title": "T", "message": "M"},
            "plain",
            mailer=mailer,
        )

    [sent] = mailer.sent_template_emails
    assert sent.template_data == {"title": "T", "message": "M"}
    assert sent.subtype is MessageType.PLAIN
    text = " ".join(record.getMessage() for record in records)
    assert ADDRESS not in text
    assert "Weekly report" not in text


async def test_s3_attachments_are_sent_under_their_file_names(
    s3: InMemoryS3Client,
) -> None:
    mailer = FakeMailer()

    await send_email_with_s3_attachments_task(
        "Report", [ADDRESS], [KEY], "See attached", mailer=mailer, s3=s3
    )

    [sent] = mailer.sent_attachment_bytes
    assert sent.attachments == [("report.pdf", b"%PDF")]
    assert sent.body_text == "See attached"
    assert sent.subtype is MessageType.PLAIN
    assert KEY in s3.objects


async def test_cleanup_deletes_the_keys_after_the_send(s3: InMemoryS3Client) -> None:
    await send_email_with_s3_attachments_task(
        "Report", [ADDRESS], [KEY], cleanup=True, mailer=FakeMailer(), s3=s3
    )

    assert KEY not in s3.objects


async def test_a_failed_send_keeps_the_keys_for_the_retry(
    s3: InMemoryS3Client,
) -> None:
    """The retry downloads the same keys again; deleting them first would
    turn one SMTP hiccup into a lost email."""
    mailer = FakeMailer()
    mailer.error = RuntimeError("smtp down")

    with pytest.raises(RuntimeError):
        await send_email_with_s3_attachments_task(
            "Report", [ADDRESS], [KEY], cleanup=True, mailer=mailer, s3=s3
        )

    assert KEY in s3.objects


async def test_a_failed_delete_after_the_send_does_not_fail_the_task(
    s3: InMemoryS3Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed task is retried, and the retry would send the email again;
    a leftover object is the cheaper outcome."""
    monkeypatch.setattr(s3, "delete_object", AsyncMock(side_effect=OSError("denied")))
    mailer = FakeMailer()

    with captured_logs("src.core.email.tasks") as records:
        await send_email_with_s3_attachments_task(
            "Report", [ADDRESS], [KEY], cleanup=True, mailer=mailer, s3=s3
        )

    assert len(mailer.sent_attachment_bytes) == 1
    messages = [record.getMessage() for record in records]
    assert "Could not delete 1 of 1 attachment(s) after the email went out" in messages
    assert not any(KEY in message for message in messages)


async def test_attachments_with_s3_off_fail_before_sending() -> None:
    """An email queued before S3 was switched off must fail loudly, not go
    out without its files."""
    mailer = FakeMailer()

    with pytest.raises(S3Disabled):
        await send_email_with_s3_attachments_task(
            "Report", [ADDRESS], [KEY], mailer=mailer, s3=DisabledS3Client()
        )

    assert mailer.sent_attachment_bytes == []


async def test_both_email_tasks_are_registered_with_retries() -> None:
    """An SMTP server or S3 that is down for a minute is the usual failure;
    without the label SmartRetryMiddleware gives up after one attempt."""
    broker = InMemoryBroker()
    register_tasks(broker, EMAIL_TASKS)

    for name in (SEND_EMAIL, SEND_EMAIL_WITH_S3_ATTACHMENTS):
        task = broker.find_task(name)
        assert task is not None
        assert task.labels["retry_on_error"] is True
