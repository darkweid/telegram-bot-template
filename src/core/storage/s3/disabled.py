"""The S3 client of a bot that stores no files."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import BinaryIO

from src.core.storage.s3.errors import S3Disabled
from src.core.storage.s3.interface import (
    DEFAULT_PART_SIZE_BYTES,
    S3Client,
    StoredObject,
)

S3_DISABLED_DETAIL = (
    "S3 is disabled: set S3_ENABLED=true and S3_BUCKET_NAME, S3_ACCESS_KEY_ID, "
    "S3_SECRET_ACCESS_KEY, S3_REGION_NAME"
)


class DisabledS3Client(S3Client):
    """Stands in for the adapter while S3_ENABLED is false: the container still
    builds, and the first call says which settings are missing."""

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def upload_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
        bucket: str | None = None,
        part_size_bytes: int = DEFAULT_PART_SIZE_BYTES,
    ) -> None:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def download_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def list_keys(
        self,
        *,
        prefix: str | None = None,
        bucket: str | None = None,
        max_keys: int | None = None,
    ) -> list[str]:
        raise S3Disabled(S3_DISABLED_DETAIL)

    def list_objects(
        self, *, prefix: str, bucket: str | None = None, page_size: int = 1000
    ) -> AsyncIterator[StoredObject]:
        # Raised on the call itself, before `async for` asks for an item.
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def head_object(
        self, key: str, *, bucket: str | None = None
    ) -> StoredObject | None:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def copy_object(
        self, source_key: str, destination_key: str, *, bucket: str | None = None
    ) -> None:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def generate_presigned_get_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        bucket: str | None = None,
    ) -> str:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def generate_presigned_put_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        bucket: str | None = None,
        content_type: str | None = None,
    ) -> str:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def object_exists(self, key: str, *, bucket: str | None = None) -> bool:
        raise S3Disabled(S3_DISABLED_DETAIL)

    async def close(self) -> None:
        """Nothing was opened; the container may still close it."""
