from __future__ import annotations

from datetime import UTC, datetime
import io
from unittest.mock import AsyncMock, Mock

from botocore.exceptions import ClientError
import pytest

from src.core.storage.s3 import adapter as adapter_module
from src.core.storage.s3.adapter import S3Adapter
from src.core.storage.s3.errors import S3ObjectTooLarge
from src.core.storage.s3.interface import StoredObject
from tests.fakes.s3 import FakeBotoClientContext, FakeBotoSession


class FakePaginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages
        self.paginate_calls: list[dict] = []

    async def paginate(self, **kwargs):  # type: ignore[no-untyped-def]
        self.paginate_calls.append(kwargs)
        for page in self._pages:
            yield page


class NonSeekableFile:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        if size is None or size < 0:
            size = len(self._data) - self._pos
        end = min(self._pos + size, len(self._data))
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk


@pytest.fixture()
def s3_mocks(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[S3Adapter, AsyncMock, FakeBotoClientContext]:
    client = AsyncMock()
    cm = FakeBotoClientContext(client)
    session = FakeBotoSession(client, cm)
    monkeypatch.setattr("src.core.storage.s3.adapter.aioboto3.Session", lambda: session)

    adapter = S3Adapter(
        bucket="default-bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=300,
    )
    return adapter, client, cm


async def test_context_manager_initializes_client(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, _, cm = s3_mocks

    async with adapter:
        assert cm.entered
    assert cm.exited


async def test_upload_and_download_bytes(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    client.get_object.return_value = {
        "Body": AsyncMock(read=AsyncMock(return_value=b"data"))
    }

    async with adapter:
        await adapter.upload_bytes("key1", b"hello", content_type="text/plain")
        data = await adapter.download_bytes("key1")

    client.put_object.assert_awaited_once_with(
        Bucket="default-bucket", Key="key1", Body=b"hello", ContentType="text/plain"
    )
    client.get_object.assert_awaited_once()
    assert data == b"data"


async def test_upload_bytes_stores_cache_control(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """S3 serves the stored value back as the Cache-Control header of every GET;
    omitting it leaves browsers revalidating each object on every view."""
    adapter, client, _ = s3_mocks

    async with adapter:
        await adapter.upload_bytes("key1", b"hello", cache_control="public, max-age=60")

    assert client.put_object.await_args.kwargs["CacheControl"] == "public, max-age=60"


async def test_list_keys_returns_keys_or_empty(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    paginator_with_keys = FakePaginator(
        [{"Contents": [{"Key": "a"}, {"Key": "b"}]}, {"Contents": [{"Key": "c"}]}]
    )
    paginator_empty = FakePaginator([{"Contents": []}])
    client.get_paginator = Mock(side_effect=[paginator_with_keys, paginator_empty])

    async with adapter:
        keys = await adapter.list_keys(prefix="pfx", max_keys=10)
        empty = await adapter.list_keys()

    assert keys == ["a", "b", "c"]
    assert empty == []
    assert client.get_paginator.call_count == 2


async def test_generate_presigned_get_url_uses_override(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    client.generate_presigned_url = Mock(return_value="http://presigned")

    async with adapter:
        url = await adapter.generate_presigned_get_url("key2", expires_in=123)

    assert url == "http://presigned"
    client.generate_presigned_url.assert_called_once()
    _, kwargs = client.generate_presigned_url.call_args
    assert kwargs["ClientMethod"] == "get_object"
    assert kwargs["ExpiresIn"] == 123


async def test_generate_presigned_put_url_includes_content_type(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    client.generate_presigned_url = Mock(return_value="http://presigned")

    async with adapter:
        url = await adapter.generate_presigned_put_url(
            "key2",
            content_type="image/png",
        )

    assert url == "http://presigned"
    _, kwargs = client.generate_presigned_url.call_args
    assert kwargs["ClientMethod"] == "put_object"
    assert kwargs["Params"]["ContentType"] == "image/png"


def test_addressing_style_default_for_endpoint() -> None:
    adapter = S3Adapter(
        bucket="bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=60,
        endpoint_url="https://s3.example.com",
    )
    assert adapter._client_config is not None
    assert adapter._client_config.s3["addressing_style"] == "path"


def test_addressing_style_default_for_aws() -> None:
    adapter = S3Adapter(
        bucket="bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=60,
        endpoint_url=None,
    )
    assert adapter._client_config is not None
    assert adapter._client_config.s3["addressing_style"] == "auto"


async def test_generate_presigned_get_url_rejects_url_key(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, _, _ = s3_mocks

    async with adapter:
        with pytest.raises(ValueError):
            await adapter.generate_presigned_get_url("https://example.com/file")


async def test_object_exists_true_and_false(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    client.head_object.side_effect = [
        {
            "ContentLength": 1,
            "ETag": '"e"',
            "LastModified": datetime(2026, 1, 1, tzinfo=UTC),
        },
        ClientError({"Error": {"Code": "404"}}, "HeadObject"),
    ]

    async with adapter:
        exists = await adapter.object_exists("key3")
        missing = await adapter.object_exists("key4")

    assert exists is True
    assert missing is False


async def test_object_exists_treats_access_denied_as_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    cm = FakeBotoClientContext(client)
    session = FakeBotoSession(client, cm)
    monkeypatch.setattr("src.core.storage.s3.adapter.aioboto3.Session", lambda: session)

    adapter = S3Adapter(
        bucket="default-bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=300,
        treat_access_denied_as_missing=True,
    )
    client.head_object.side_effect = [
        ClientError({"Error": {"Code": "403"}}, "HeadObject"),
    ]

    async with adapter:
        missing = await adapter.object_exists("key4")

    assert missing is False


async def test_delete_object(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks

    async with adapter:
        await adapter.delete_object("key5")

    client.delete_object.assert_awaited_once_with(Bucket="default-bucket", Key="key5")


def test_round_up_to_megabyte() -> None:
    adapter = S3Adapter(
        bucket="bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=60,
    )
    assert adapter._round_up_to_megabyte(5 * 1024 * 1024 + 1) == 6 * 1024 * 1024


async def test_list_objects_yields_metadata_page_by_page(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    paginator = FakePaginator(
        [
            {
                "Contents": [
                    {"Key": "p/a", "Size": 3, "ETag": '"e1"', "LastModified": stamp}
                ]
            },
            {
                "Contents": [
                    {"Key": "p/b", "Size": 5, "ETag": '"e2"', "LastModified": stamp}
                ]
            },
            {},
        ]
    )
    client.get_paginator = Mock(return_value=paginator)

    async with adapter:
        objects = [
            item async for item in adapter.list_objects(prefix="p/", page_size=2)
        ]

    assert objects == [
        StoredObject(key="p/a", size=3, etag="e1", last_modified=stamp),
        StoredObject(key="p/b", size=5, etag="e2", last_modified=stamp),
    ]
    assert paginator.paginate_calls == [
        {
            "Bucket": "default-bucket",
            "Prefix": "p/",
            "PaginationConfig": {"PageSize": 2},
        }
    ]


async def test_list_objects_survives_empty_and_missing_contents(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """S3 omits `Contents` on an empty listing and a page may carry None."""
    adapter, client, _ = s3_mocks
    client.get_paginator = Mock(
        return_value=FakePaginator([{}, {"Contents": None}, {"Contents": []}])
    )

    async with adapter:
        objects = [item async for item in adapter.list_objects(prefix="none/")]

    assert objects == []


async def test_list_objects_honours_bucket_override_and_default_page_size(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    paginator = FakePaginator([])
    client.get_paginator = Mock(return_value=paginator)

    async with adapter:
        _ = [item async for item in adapter.list_objects(prefix="p/", bucket="other")]

    assert paginator.paginate_calls == [
        {"Bucket": "other", "Prefix": "p/", "PaginationConfig": {"PageSize": 1000}}
    ]


async def test_list_objects_keeps_unquoted_and_inner_quote_free_etag(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """A multipart ETag (`hash-3`) and an unquoted one both come out unwrapped."""
    adapter, client, _ = s3_mocks
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    client.get_paginator = Mock(
        return_value=FakePaginator(
            [
                {
                    "Contents": [
                        {
                            "Key": "a",
                            "Size": 1,
                            "ETag": '"abc-3"',
                            "LastModified": stamp,
                        },
                        {"Key": "b", "Size": 1, "ETag": "plain", "LastModified": stamp},
                    ]
                }
            ]
        )
    )

    async with adapter:
        etags = [item.etag async for item in adapter.list_objects(prefix="")]

    assert etags == ["abc-3", "plain"]


@pytest.mark.parametrize("page_size", [0, -1, 1001])
async def test_list_objects_refuses_page_size_outside_bounds(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext], page_size: int
) -> None:
    adapter, client, _ = s3_mocks
    client.get_paginator = Mock()

    async with adapter:
        with pytest.raises(ValueError):
            _ = [
                item
                async for item in adapter.list_objects(prefix="p/", page_size=page_size)
            ]

    client.get_paginator.assert_not_called()


@pytest.mark.parametrize("page_size", [1, 1000])
async def test_list_objects_accepts_page_size_bounds(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext], page_size: int
) -> None:
    adapter, client, _ = s3_mocks
    paginator = FakePaginator([])
    client.get_paginator = Mock(return_value=paginator)

    async with adapter:
        _ = [
            item
            async for item in adapter.list_objects(prefix="p/", page_size=page_size)
        ]

    assert paginator.paginate_calls[0]["PaginationConfig"] == {"PageSize": page_size}


async def test_head_object_returns_metadata_or_none(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    stamp = datetime(2026, 1, 1, tzinfo=UTC)
    client.head_object.side_effect = [
        {"ContentLength": 7, "ETag": '"abc"', "LastModified": stamp},
        ClientError({"Error": {"Code": "404"}}, "HeadObject"),
    ]

    async with adapter:
        found = await adapter.head_object("k1")
        missing = await adapter.head_object("k2")

    assert found == StoredObject(key="k1", size=7, etag="abc", last_modified=stamp)
    assert missing is None


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
async def test_head_object_treats_every_missing_code_as_none(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext], code: str
) -> None:
    adapter, client, _ = s3_mocks
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": code}}, "HeadObject"
    )

    async with adapter:
        assert await adapter.head_object("k") is None


async def test_head_object_raises_on_other_errors(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "500"}}, "HeadObject"
    )

    async with adapter:
        with pytest.raises(ClientError):
            await adapter.head_object("k1")


async def test_head_object_raises_on_access_denied_by_default(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """Hiding a permissions problem as 'missing' would make a cleanup job
    believe objects are gone; it is opt-in."""
    adapter, client, _ = s3_mocks
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "403"}}, "HeadObject"
    )

    async with adapter:
        with pytest.raises(ClientError):
            await adapter.head_object("k1")


async def test_head_object_access_denied_is_none_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncMock()
    session = FakeBotoSession(client, FakeBotoClientContext(client))
    monkeypatch.setattr("src.core.storage.s3.adapter.aioboto3.Session", lambda: session)
    adapter = S3Adapter(
        bucket="default-bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=300,
        treat_access_denied_as_missing=True,
    )
    client.head_object.side_effect = ClientError(
        {"Error": {"Code": "403"}}, "HeadObject"
    )

    async with adapter:
        assert await adapter.head_object("k1") is None


async def test_copy_object_copies_server_side_with_metadata(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks

    async with adapter:
        await adapter.copy_object("a/src", "trash/a/src")

    client.copy_object.assert_awaited_once_with(
        Bucket="default-bucket",
        Key="trash/a/src",
        CopySource={"Bucket": "default-bucket", "Key": "a/src"},
        MetadataDirective="COPY",
    )


async def test_copy_object_uses_bucket_override_for_both_sides(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    adapter, client, _ = s3_mocks

    async with adapter:
        await adapter.copy_object("a", "b", bucket="other")

    client.copy_object.assert_awaited_once_with(
        Bucket="other",
        Key="b",
        CopySource={"Bucket": "other", "Key": "a"},
        MetadataDirective="COPY",
    )


async def test_copy_object_propagates_missing_source(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """Unlike HEAD, a copy has no 'missing' answer: the caller must not move
    on to delete the original after a copy that did not happen."""
    adapter, client, _ = s3_mocks
    client.copy_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "CopyObject"
    )

    async with adapter:
        with pytest.raises(ClientError):
            await adapter.copy_object("gone", "trash/gone")


async def test_copy_object_onto_itself_is_refused_before_the_call(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """S3 answers InvalidRequest for a self-copy with unchanged metadata."""
    adapter, client, _ = s3_mocks

    async with adapter:
        with pytest.raises(ValueError):
            await adapter.copy_object("same", "same")

    client.copy_object.assert_not_awaited()


def small_part_adapter(
    monkeypatch: pytest.MonkeyPatch, *, max_upload_size_bytes: int = 100
) -> tuple[S3Adapter, AsyncMock]:
    """An adapter whose parts are bytes, not mebibytes, so a test streams a
    handful of bytes through every branch."""
    client = AsyncMock()
    session = FakeBotoSession(client, FakeBotoClientContext(client))
    monkeypatch.setattr(adapter_module.aioboto3, "Session", lambda: session)
    monkeypatch.setattr(adapter_module, "MIN_MULTIPART_PART_SIZE_BYTES", 5)
    monkeypatch.setattr(
        S3Adapter, "_round_up_to_megabyte", lambda self, size_bytes: size_bytes
    )
    client.create_multipart_upload.return_value = {"UploadId": "u1"}
    client.upload_part.side_effect = lambda **kwargs: {
        "ETag": f"e{kwargs['PartNumber']}"
    }
    adapter = S3Adapter(
        bucket="default-bucket",
        region="us-east-1",
        access_key="ak",
        secret_key="sk",
        default_presign_ttl=300,
        max_upload_size_bytes=max_upload_size_bytes,
    )
    return adapter, client


async def test_a_stream_within_one_part_is_one_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multipart upload costs three requests; a voice note needs one."""
    adapter, client = small_part_adapter(monkeypatch)

    async with adapter:
        await adapter.upload_stream(
            "voice.ogg",
            io.BytesIO(b"abc"),
            content_type="audio/ogg",
            cache_control="no-cache",
            part_size_bytes=5,
        )

    client.put_object.assert_awaited_once_with(
        Bucket="default-bucket",
        Key="voice.ogg",
        Body=b"abc",
        ContentType="audio/ogg",
        CacheControl="no-cache",
    )
    client.create_multipart_upload.assert_not_awaited()


async def test_a_stream_of_exactly_one_part_is_still_one_put(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full first part says nothing about what follows: the adapter must look
    once more before it opens a multipart upload with a single part."""
    adapter, client = small_part_adapter(monkeypatch)

    async with adapter:
        await adapter.upload_stream("k", io.BytesIO(b"01234"), part_size_bytes=5)

    assert client.put_object.await_args.kwargs["Body"] == b"01234"
    client.create_multipart_upload.assert_not_awaited()


async def test_an_empty_stream_stores_an_empty_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = small_part_adapter(monkeypatch)

    async with adapter:
        await adapter.upload_stream("empty", io.BytesIO(b""), part_size_bytes=5)

    assert client.put_object.await_args.kwargs["Body"] == b""


async def test_a_longer_stream_goes_up_part_by_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parts keep their order and numbers; S3 assembles the object from the
    list given to complete_multipart_upload, not from arrival order."""
    adapter, client = small_part_adapter(monkeypatch)

    async with adapter:
        await adapter.upload_stream(
            "big",
            NonSeekableFile(b"0123456789AB"),
            content_type="application/pdf",
            cache_control="no-cache",
            part_size_bytes=5,
        )

    create = client.create_multipart_upload.await_args.kwargs
    assert create == {
        "Bucket": "default-bucket",
        "Key": "big",
        "ContentType": "application/pdf",
        "CacheControl": "no-cache",
    }
    bodies = [call.kwargs["Body"] for call in client.upload_part.await_args_list]
    assert bodies == [b"01234", b"56789", b"AB"]
    complete = client.complete_multipart_upload.await_args.kwargs
    assert complete["MultipartUpload"] == {
        "Parts": [
            {"ETag": "e1", "PartNumber": 1},
            {"ETag": "e2", "PartNumber": 2},
            {"ETag": "e3", "PartNumber": 3},
        ]
    }
    client.put_object.assert_not_awaited()


async def test_a_stream_over_the_limit_in_one_part_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = small_part_adapter(monkeypatch, max_upload_size_bytes=8)

    async with adapter:
        with pytest.raises(S3ObjectTooLarge):
            await adapter.upload_stream(
                "k", io.BytesIO(b"012345678"), part_size_bytes=10
            )

    client.put_object.assert_not_awaited()


async def test_a_stream_over_the_limit_aborts_the_multipart_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unaborted upload keeps its parts in the bucket, billed, forever."""
    adapter, client = small_part_adapter(monkeypatch, max_upload_size_bytes=12)

    async with adapter:
        with pytest.raises(S3ObjectTooLarge):
            await adapter.upload_stream(
                "k", io.BytesIO(b"0123456789ABCDE"), part_size_bytes=5
            )

    client.abort_multipart_upload.assert_awaited_once_with(
        Bucket="default-bucket", Key="k", UploadId="u1"
    )
    client.complete_multipart_upload.assert_not_awaited()


async def test_a_failed_part_aborts_the_upload_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, client = small_part_adapter(monkeypatch)
    client.upload_part.side_effect = ClientError(
        {"Error": {"Code": "InternalError"}}, "UploadPart"
    )

    async with adapter:
        with pytest.raises(ClientError):
            await adapter.upload_stream(
                "k", io.BytesIO(b"0123456789"), part_size_bytes=5
            )

    client.abort_multipart_upload.assert_awaited_once()
    client.complete_multipart_upload.assert_not_awaited()


async def test_a_part_below_the_s3_minimum_is_refused(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """S3 rejects every part but the last below 5 MiB, after the upload began."""
    adapter, client, _ = s3_mocks

    async with adapter:
        with pytest.raises(ValueError, match="5 MiB"):
            await adapter.upload_stream("k", io.BytesIO(b"x"), part_size_bytes=1024)

    client.put_object.assert_not_awaited()


async def test_an_adapter_used_outside_its_context_says_so(
    s3_mocks: tuple[S3Adapter, AsyncMock, FakeBotoClientContext],
) -> None:
    """Without `async with` there is no client; the error must name the fix
    rather than an AttributeError on None."""
    adapter, _, _ = s3_mocks

    with pytest.raises(RuntimeError, match="async with"):
        await adapter.download_bytes("k")


async def test_parts_grow_so_the_largest_upload_fits_into_the_part_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With 10 000 parts at most, a small part size and a large limit would fail
    near the end of a long upload; the adapter raises the part size instead."""
    monkeypatch.setattr(adapter_module, "MAX_MULTIPART_PARTS", 2)
    adapter, client = small_part_adapter(monkeypatch, max_upload_size_bytes=30)

    async with adapter:
        await adapter.upload_stream(
            "k", io.BytesIO(b"0123456789ABCDEFGHIJ"), part_size_bytes=5
        )

    bodies = [call.kwargs["Body"] for call in client.upload_part.await_args_list]
    assert bodies == [b"0123456789ABCDE", b"FGHIJ"]
