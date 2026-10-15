import io

import pytest

from src.core.storage.s3.disabled import DisabledS3Client
from src.core.storage.s3.errors import S3Disabled


async def test_every_call_names_the_settings_to_turn_s3_on() -> None:
    """The developer sees which keys are missing in the first error, not a
    NoneType deep inside botocore."""
    client = DisabledS3Client()
    calls = [
        client.upload_bytes("k", b"x"),
        client.upload_stream("k", io.BytesIO(b"x")),
        client.download_bytes("k"),
        client.delete_object("k"),
        client.list_keys(),
        client.head_object("k"),
        client.copy_object("a", "b"),
        client.generate_presigned_get_url("k"),
        client.generate_presigned_put_url("k"),
        client.object_exists("k"),
    ]
    for call in calls:
        with pytest.raises(S3Disabled, match="S3_ENABLED=true"):
            await call


def test_listing_fails_before_the_first_item() -> None:
    with pytest.raises(S3Disabled):
        DisabledS3Client().list_objects(prefix="")


async def test_closing_a_disabled_client_is_harmless() -> None:
    """The container closes whatever S3 client it built, enabled or not."""
    await DisabledS3Client().close()
