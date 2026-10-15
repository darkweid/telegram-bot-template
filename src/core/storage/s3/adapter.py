from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import math
from types import TracebackType
from typing import Any, BinaryIO, Self

import aioboto3
from botocore.config import Config
from botocore.exceptions import ClientError

from src.core.storage.s3.errors import S3ObjectTooLarge
from src.core.storage.s3.interface import (
    DEFAULT_PART_SIZE_BYTES,
    S3Client,
    StoredObject,
)

MIN_MULTIPART_PART_SIZE_BYTES = 5 * 1024 * 1024
MAX_MULTIPART_PARTS = 10_000
MAX_LIST_PAGE_SIZE = 1000


class S3Adapter(S3Client):
    """
    Async S3 adapter over aioboto3.

    Usage:
    async with S3Adapter(...) as s3:
        await s3.upload_bytes("key", b"data")

    `upload_stream` takes a file of any size up to `max_upload_size_bytes`,
    a part at a time; `download_bytes` holds the whole object in memory. One
    call can take minutes (read timeout times retries), longer than the 60 s
    event-isolation lock and the shutdown timeout: run large transfers in a
    background task, not in a handler.
    """

    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        default_presign_ttl: int,
        endpoint_url: str | None = None,
        addressing_style: str | None = None,
        signature_version: str = "s3v4",
        verify_ssl: bool = True,
        ca_bundle: str | None = None,
        treat_access_denied_as_missing: bool = False,
        connect_timeout_seconds: int = 5,
        read_timeout_seconds: int = 60,
        retry_max_attempts: int = 3,
        retry_mode: str = "standard",
        max_upload_size_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._default_presign_ttl = default_presign_ttl
        self._endpoint_url = endpoint_url
        self._verify_ssl = verify_ssl
        self._ca_bundle = ca_bundle
        self._treat_access_denied_as_missing = treat_access_denied_as_missing
        self._max_upload_size_bytes = max_upload_size_bytes
        # The smallest part that still fits the largest allowed upload into
        # S3's 10 000 parts, so a stream within the limit never runs out of them.
        self._min_part_size_bytes = self._round_up_to_megabyte(
            max(
                MIN_MULTIPART_PART_SIZE_BYTES,
                math.ceil(max_upload_size_bytes / MAX_MULTIPART_PARTS),
            )
        )
        self._session = aioboto3.Session()
        self._client_cm: Any = None
        self._client: Any = None
        self._client_config = self._build_client_config(
            signature_version=signature_version,
            addressing_style=addressing_style,
            endpoint_url=endpoint_url,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            retry_max_attempts=retry_max_attempts,
            retry_mode=retry_mode,
        )

    async def __aenter__(self) -> Self:
        client_kwargs: dict[str, Any] = {
            "region_name": self._region,
            "aws_access_key_id": self._access_key,
            "aws_secret_access_key": self._secret_key,
        }
        if self._endpoint_url:
            client_kwargs["endpoint_url"] = self._endpoint_url
        client_kwargs["config"] = self._client_config
        if self._ca_bundle:
            client_kwargs["verify"] = self._ca_bundle
        else:
            client_kwargs["verify"] = self._verify_ssl
        self._client_cm = self._session.client("s3", **client_kwargs)
        self._client = await self._client_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close(exc_type, exc, tb)

    def _get_bucket(self, bucket: str | None) -> str:
        return bucket or self._bucket

    def _build_client_config(
        self,
        *,
        signature_version: str,
        addressing_style: str | None,
        endpoint_url: str | None,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
        retry_max_attempts: int,
        retry_mode: str,
    ) -> Config:
        config_kwargs: dict[str, Any] = {}
        config_kwargs["signature_version"] = signature_version
        resolved_addressing_style = self._resolve_addressing_style(
            addressing_style=addressing_style,
            endpoint_url=endpoint_url,
        )
        config_kwargs["s3"] = {"addressing_style": resolved_addressing_style}
        config_kwargs["connect_timeout"] = connect_timeout_seconds
        config_kwargs["read_timeout"] = read_timeout_seconds
        config_kwargs["retries"] = {
            "max_attempts": retry_max_attempts,
            "mode": retry_mode,
        }
        return Config(**config_kwargs)

    def _resolve_addressing_style(
        self,
        *,
        addressing_style: str | None,
        endpoint_url: str | None,
    ) -> str:
        if addressing_style:
            return addressing_style
        if endpoint_url:
            return "path"
        return "auto"

    def _ensure_client(self) -> Any:
        if self._client is None:
            raise RuntimeError(
                "S3 client is not initialized. Use 'async with S3Adapter(...):'."
            )
        return self._client

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        *,
        bucket: str | None = None,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Store `data` under `key`.

        `cache_control` is saved as object metadata and served back as the
        `Cache-Control` header of every GET of the object.
        """
        client = self._ensure_client()
        put_kwargs: dict[str, Any] = {
            "Bucket": self._get_bucket(bucket),
            "Key": key,
            "Body": data,
        }
        if content_type:
            put_kwargs["ContentType"] = content_type
        if cache_control:
            put_kwargs["CacheControl"] = cache_control
        await client.put_object(**put_kwargs)

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
        """Store what `stream` yields from its current position under `key`.

        One PUT when the stream fits into one part, a multipart upload
        otherwise. The stream is read a part at a time and needs neither seek
        nor a known size. Past `max_upload_size_bytes` it raises
        S3ObjectTooLarge, and the parts already sent are discarded. A part
        smaller than the limit allows is raised to the smallest one that fits
        the largest upload into S3's 10 000 parts.
        """
        if part_size_bytes < MIN_MULTIPART_PART_SIZE_BYTES:
            raise ValueError("Multipart part size must be at least 5 MiB.")
        # Before the first read: outside `async with` nothing may be consumed.
        self._ensure_client()
        part_size = max(part_size_bytes, self._min_part_size_bytes)
        first = await self._read_part(stream, part_size, received=0)
        second = (
            await self._read_part(stream, part_size, received=len(first))
            if len(first) == part_size
            else b""
        )
        if not second:
            await self.upload_bytes(
                key,
                first,
                bucket=bucket,
                content_type=content_type,
                cache_control=cache_control,
            )
            return
        await self._upload_multipart(
            key,
            stream,
            [first, second],
            part_size=part_size,
            bucket=bucket,
            content_type=content_type,
            cache_control=cache_control,
        )

    async def _read_part(self, stream: BinaryIO, size: int, *, received: int) -> bytes:
        """Read `size` bytes, fewer only at the end of the stream.

        A raw or unbuffered stream (a pipe, a socket file) may return less than
        asked on any read, so one `read(size)` would cut an upload short or send
        an undersized part. The limit is checked on the running total.
        """
        chunks: list[bytes] = []
        length = 0
        while length < size:
            # In a thread: reading a file on disk would otherwise block the loop.
            chunk = await asyncio.to_thread(stream.read, size - length)
            if not chunk:
                break
            length += len(chunk)
            if received + length > self._max_upload_size_bytes:
                raise S3ObjectTooLarge(
                    "Upload exceeds "
                    f"S3_MAX_UPLOAD_SIZE_BYTES={self._max_upload_size_bytes}"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def _upload_multipart(
        self,
        key: str,
        stream: BinaryIO,
        head: list[bytes],
        *,
        part_size: int,
        bucket: str | None,
        content_type: str | None,
        cache_control: str | None,
    ) -> None:
        client = self._ensure_client()
        bucket_name = self._get_bucket(bucket)
        create_kwargs: dict[str, Any] = {"Bucket": bucket_name, "Key": key}
        if content_type:
            create_kwargs["ContentType"] = content_type
        if cache_control:
            create_kwargs["CacheControl"] = cache_control
        response = await client.create_multipart_upload(**create_kwargs)
        upload_id = response["UploadId"]
        parts: list[dict[str, Any]] = []
        pending = list(head)
        received = sum(len(part) for part in head)
        try:
            while pending:
                part_number = len(parts) + 1
                uploaded = await client.upload_part(
                    Bucket=bucket_name,
                    Key=key,
                    PartNumber=part_number,
                    UploadId=upload_id,
                    Body=pending.pop(0),
                )
                parts.append({"ETag": uploaded["ETag"], "PartNumber": part_number})
                if not pending:
                    chunk = await self._read_part(stream, part_size, received=received)
                    received += len(chunk)
                    if chunk:
                        pending.append(chunk)
            await client.complete_multipart_upload(
                Bucket=bucket_name,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except BaseException as error:
            # An unfinished multipart upload keeps its parts, and S3 bills
            # them, until it is aborted. BaseException: a cancelled task (a
            # stopping worker or bot) leaves the same orphan.
            await self._abort_multipart(client, bucket_name, key, upload_id, error)
            raise

    async def _abort_multipart(
        self, client: Any, bucket: str, key: str, upload_id: str, error: BaseException
    ) -> None:
        """Abort without ever replacing `error`: a failed abort is attached to it
        as a note (the class name only) and the caller re-raises the original."""
        try:
            # Shielded: the cancellation that got us here must not cut the abort.
            await asyncio.shield(
                client.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            )
        except Exception as abort_error:
            error.add_note(
                f"abort_multipart_upload failed: {type(abort_error).__name__}"
            )

    async def download_bytes(self, key: str, *, bucket: str | None = None) -> bytes:
        """Download an object into memory (small files only)."""
        client = self._ensure_client()
        response = await client.get_object(Bucket=self._get_bucket(bucket), Key=key)
        body = response["Body"]
        data: bytes = await body.read()
        return data

    def _round_up_to_megabyte(self, size_bytes: int) -> int:
        megabyte = 1024 * 1024
        return math.ceil(size_bytes / megabyte) * megabyte

    async def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        client = self._ensure_client()
        await client.delete_object(Bucket=self._get_bucket(bucket), Key=key)

    async def list_keys(
        self,
        *,
        prefix: str | None = None,
        bucket: str | None = None,
        max_keys: int | None = None,
    ) -> list[str]:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {"Bucket": self._get_bucket(bucket)}
        if prefix:
            kwargs["Prefix"] = prefix
        if max_keys is not None and max_keys <= 0:
            return []

        pagination_config: dict[str, int] = {}
        if max_keys is not None:
            pagination_config["MaxItems"] = max_keys
            pagination_config["PageSize"] = min(max_keys, 1000)

        paginator = client.get_paginator("list_objects_v2")
        paginate_kwargs = dict(kwargs)
        if pagination_config:
            paginate_kwargs["PaginationConfig"] = pagination_config

        keys: list[str] = []
        async for page in paginator.paginate(**paginate_kwargs):
            contents = page.get("Contents") or []
            keys.extend(item["Key"] for item in contents if "Key" in item)
        return keys

    async def generate_presigned_get_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        bucket: str | None = None,
    ) -> str:
        return await self._generate_presigned_url(
            key=key,
            bucket=bucket,
            expires_in=expires_in,
            method="get_object",
            content_type=None,
        )

    async def generate_presigned_put_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        bucket: str | None = None,
        content_type: str | None = None,
    ) -> str:
        return await self._generate_presigned_url(
            key=key,
            bucket=bucket,
            expires_in=expires_in,
            method="put_object",
            content_type=content_type,
        )

    async def _generate_presigned_url(
        self,
        *,
        key: str,
        bucket: str | None,
        expires_in: int | None,
        method: str,
        content_type: str | None,
    ) -> str:
        client = self._ensure_client()
        if "://" in key:
            raise ValueError("S3 object key must not be a URL or contain a scheme.")
        params = {"Bucket": self._get_bucket(bucket), "Key": key}
        if content_type:
            params["ContentType"] = content_type
        ttl = expires_in or self._default_presign_ttl

        url_or_coro = client.generate_presigned_url(
            ClientMethod=method,
            Params=params,
            ExpiresIn=ttl,
        )
        if asyncio.iscoroutine(url_or_coro):
            return str(await url_or_coro)
        return str(url_or_coro)

    def _is_missing(self, exc: ClientError) -> bool:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return True
        return self._treat_access_denied_as_missing and error_code in {
            "403",
            "AccessDenied",
            "Forbidden",
        }

    async def object_exists(self, key: str, *, bucket: str | None = None) -> bool:
        return await self.head_object(key, bucket=bucket) is not None

    async def list_objects(
        self, *, prefix: str, bucket: str | None = None, page_size: int = 1000
    ) -> AsyncIterator[StoredObject]:
        """Every object under the prefix, a page at a time: a large bucket is
        never held in memory as one list.

        `page_size` must be within 1..1000, S3's per-request maximum.
        """
        if not 1 <= page_size <= MAX_LIST_PAGE_SIZE:
            raise ValueError(f"page_size must be within 1..{MAX_LIST_PAGE_SIZE}.")
        client = self._ensure_client()
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=self._get_bucket(bucket),
            Prefix=prefix,
            PaginationConfig={"PageSize": page_size},
        ):
            for item in page.get("Contents") or []:
                yield StoredObject(
                    key=item["Key"],
                    size=int(item["Size"]),
                    etag=str(item["ETag"]).strip('"'),
                    last_modified=item["LastModified"],
                )

    async def head_object(
        self, key: str, *, bucket: str | None = None
    ) -> StoredObject | None:
        """The object's metadata, or None when the bucket has no such key.

        Other failures (and access denied, unless configured as missing) raise.
        """
        client = self._ensure_client()
        try:
            response = await client.head_object(
                Bucket=self._get_bucket(bucket), Key=key
            )
        except ClientError as exc:
            if self._is_missing(exc):
                return None
            raise
        return StoredObject(
            key=key,
            size=int(response["ContentLength"]),
            etag=str(response["ETag"]).strip('"'),
            last_modified=response["LastModified"],
        )

    async def copy_object(
        self, source_key: str, destination_key: str, *, bucket: str | None = None
    ) -> None:
        """Server-side copy inside one bucket that keeps the content type and
        Cache-Control.

        A missing source raises the provider's ClientError (NoSuchKey), it is
        not reported as a quiet no-op. Copying a key onto itself is refused
        here because S3 itself rejects it.

        A single CopyObject takes sources up to 5 GB: a larger one is refused by
        S3 and surfaces as ClientError, so a copy that did not happen never reads
        as success. Multipart copy is not implemented.
        """
        if source_key == destination_key:
            raise ValueError("S3 copy source and destination must differ.")
        client = self._ensure_client()
        name = self._get_bucket(bucket)
        await client.copy_object(
            Bucket=name,
            Key=destination_key,
            CopySource={"Bucket": name, "Key": source_key},
            MetadataDirective="COPY",
        )

    async def close(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        client_cm = self._client_cm
        self._client = None
        self._client_cm = None
        if client_cm:
            await client_cm.__aexit__(exc_type, exc, tb)
