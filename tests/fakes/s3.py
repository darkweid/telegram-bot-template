from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
import hashlib
from typing import Any, BinaryIO
from unittest.mock import AsyncMock

from botocore.exceptions import ClientError

from src.core.storage.s3.adapter import MAX_LIST_PAGE_SIZE
from src.core.storage.s3.interface import DEFAULT_PART_SIZE_BYTES, StoredObject
from src.core.utils.datetime_utils import get_utc_now


class FakeBotoClientContext:
    """What `aioboto3.Session().client("s3")` returns: entering it yields the
    client."""

    def __init__(self, client: AsyncMock) -> None:
        self._client = client
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> AsyncMock:
        self.entered = True
        return self._client

    async def __aexit__(self, *exc_info: object) -> None:
        self.exited = True


class FakeBotoSession:
    """Stands in for `aioboto3.Session`; records how the client was asked for."""

    def __init__(self, client: AsyncMock, context: FakeBotoClientContext) -> None:
        self._client = client
        self._context = context
        self.client_calls: list[dict[str, Any]] = []

    def client(self, *args: Any, **kwargs: Any) -> FakeBotoClientContext:
        self.client_calls.append({"args": args, "kwargs": kwargs})
        return self._context


class InMemoryS3Client:
    """The S3Client protocol over dicts, one per bucket. Timestamps come from
    `get_utc_now`, which a test monkeypatches through this module."""

    def __init__(self, default_bucket: str = "test-bucket") -> None:
        self._default_bucket = default_bucket
        self._buckets: dict[str, dict[str, bytes]] = {}
        self._modified: dict[str, dict[str, datetime]] = {}
        self.content_types: dict[str, str | None] = {}
        self.closed = False

    def _get_modified(self, bucket: str | None) -> dict[str, datetime]:
        return self._modified.setdefault(bucket or self._default_bucket, {})

    def _stored(self, key: str, bucket: str | None) -> StoredObject:
        data = self._get_bucket(bucket)[key]
        return StoredObject(
            key=key,
            size=len(data),
            etag=hashlib.md5(data, usedforsecurity=False).hexdigest(),
            last_modified=self._get_modified(bucket)[key],
        )

    def set_last_modified(
        self, key: str, value: datetime, *, bucket: str | None = None
    ) -> None:
        if value.tzinfo is None:
            raise ValueError("last_modified must be timezone-aware.")
        if key not in self._get_bucket(bucket):
            raise FileNotFoundError(f"Object not found: {key}")
        self._get_modified(bucket)[key] = value

    def _get_bucket(self, bucket: str | None) -> dict[str, bytes]:
        return self._buckets.setdefault(bucket or self._default_bucket, {})

    @property
    def objects(self) -> dict[str, bytes]:
        """Contents of the default bucket, keyed by object key."""
        return self._get_bucket(None)

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        self._get_bucket(bucket)[key] = data
        self._get_modified(bucket)[key] = get_utc_now()
        self.content_types[key] = content_type

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
        await self.upload_bytes(
            key,
            stream.read(),
            bucket=bucket,
            content_type=content_type,
            cache_control=cache_control,
        )

    async def download_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        source = self._get_bucket(bucket)
        if key not in source:
            raise FileNotFoundError(f"Object not found: {key}")
        return source[key]

    async def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        self._get_bucket(bucket).pop(key, None)
        self._get_modified(bucket).pop(key, None)

    async def list_keys(
        self,
        *,
        prefix: str | None = None,
        bucket: str | None = None,
        max_keys: int | None = None,
    ) -> list[str]:
        keys = list(self._get_bucket(bucket))
        if prefix:
            keys = [key for key in keys if key.startswith(prefix)]
        if max_keys is not None:
            keys = keys[:max_keys]
        return keys

    async def generate_presigned_get_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        bucket: str | None = None,
    ) -> str:
        name = bucket or self._default_bucket
        return f"https://s3.local/{name}/{key}?op=get&expires_in={expires_in or 0}"

    async def generate_presigned_put_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        bucket: str | None = None,
        content_type: str | None = None,
    ) -> str:
        name = bucket or self._default_bucket
        return f"https://s3.local/{name}/{key}?op=put&expires_in={expires_in or 0}"

    async def object_exists(self, key: str, *, bucket: str | None = None) -> bool:
        return key in self._get_bucket(bucket)

    async def list_objects(
        self, *, prefix: str, bucket: str | None = None, page_size: int = 1000
    ) -> AsyncIterator[StoredObject]:
        if not 1 <= page_size <= MAX_LIST_PAGE_SIZE:
            raise ValueError(f"page_size must be within 1..{MAX_LIST_PAGE_SIZE}.")
        for key in sorted(self._get_bucket(bucket)):
            if key.startswith(prefix):
                yield self._stored(key, bucket)

    async def head_object(
        self, key: str, *, bucket: str | None = None
    ) -> StoredObject | None:
        if key not in self._get_bucket(bucket):
            return None
        return self._stored(key, bucket)

    async def copy_object(
        self, source_key: str, destination_key: str, *, bucket: str | None = None
    ) -> None:
        if source_key == destination_key:
            raise ValueError("S3 copy source and destination must differ.")
        source = self._get_bucket(bucket)
        if source_key not in source:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Object not found"}},
                "CopyObject",
            )
        source[destination_key] = source[source_key]
        self._get_modified(bucket)[destination_key] = get_utc_now()

    async def close(self) -> None:
        self.closed = True
