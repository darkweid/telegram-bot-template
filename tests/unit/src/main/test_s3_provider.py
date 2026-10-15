from unittest.mock import AsyncMock

from pydantic import SecretStr
import pytest

from src.core.storage.s3 import adapter as adapter_module
from src.core.storage.s3.adapter import S3Adapter
from src.core.storage.s3.disabled import DisabledS3Client
from src.core.storage.s3.errors import S3Disabled
from src.core.storage.s3.interface import S3Client
from src.main.config import Config, S3Config
from src.main.di import build_container, build_s3_adapter
from tests.fakes.s3 import FakeBotoClientContext, FakeBotoSession


def enabled_s3(**overrides: object) -> S3Config:
    values: dict[str, object] = {
        "S3_ENABLED": True,
        "S3_BUCKET_NAME": "bucket",
        "S3_REGION_NAME": "us-east-1",
        "S3_ACCESS_KEY_ID": "ak",
        "S3_SECRET_ACCESS_KEY": SecretStr("sk"),
        "S3_PRE_SIGNED_URL_SECONDS": 123,
    }
    values.update(overrides)
    return S3Config(**values)


@pytest.fixture
def boto(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FakeBotoSession, FakeBotoClientContext]:
    client = AsyncMock()
    context = FakeBotoClientContext(client)
    session = FakeBotoSession(client, context)
    monkeypatch.setattr(adapter_module.aioboto3, "Session", lambda: session)
    return session, context


async def test_the_adapter_gets_every_setting(
    boto: tuple[FakeBotoSession, FakeBotoClientContext],
) -> None:
    """A setting dropped on the way to botocore is a silent default in prod."""
    session, context = boto
    adapter = build_s3_adapter(
        enabled_s3(S3_ENDPOINT_URL="https://minio.local", S3_CA_BUNDLE="/ca.pem")
    )

    async with adapter:
        assert context.entered

    assert context.exited
    [call] = session.client_calls
    assert call["args"] == ("s3",)
    kwargs = call["kwargs"]
    assert kwargs["region_name"] == "us-east-1"
    assert kwargs["aws_access_key_id"] == "ak"
    assert kwargs["aws_secret_access_key"] == "sk"
    assert kwargs["endpoint_url"] == "https://minio.local"
    assert kwargs["verify"] == "/ca.pem"
    config = kwargs["config"]
    assert config.signature_version == "s3v4"
    assert config.s3 == {"addressing_style": "path"}
    assert config.connect_timeout == 5
    assert config.read_timeout == 120
    assert config.retries == {"total_max_attempts": 3, "mode": "standard"}


async def test_with_s3_off_the_container_holds_the_disabled_client(
    settings: Config,
) -> None:
    """STRICT_VALIDATION builds the container without any S3 key, and a call
    explains itself instead of failing inside botocore."""
    container = build_container(settings)
    try:
        client = await container.get(S3Client)
        assert isinstance(client, DisabledS3Client)
        with pytest.raises(S3Disabled):
            await client.download_bytes("k")
    finally:
        await container.close()


async def test_the_enabled_client_opens_once_and_closes_with_the_container(
    settings: Config, boto: tuple[FakeBotoSession, FakeBotoClientContext]
) -> None:
    """The bot and the worker both release S3 by closing their container; a
    client left open leaks its connection pool on every restart."""
    session, context = boto
    container = build_container(settings.model_copy(update={"s3": enabled_s3()}))
    try:
        first = await container.get(S3Client)
        second = await container.get(S3Client)
        assert isinstance(first, S3Adapter)
        assert first is second
        assert context.entered and not context.exited
    finally:
        await container.close()
    assert context.exited
    assert len(session.client_calls) == 1
