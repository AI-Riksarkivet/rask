"""The S3 adapters answer in storage's OWN taxonomy, and there is one of each mechanism.

PS-02 — `storage.errors` exists so that no caller of this package ever imports botocore, and the
package applied it to NOTHING: `s3_errors` appeared only in its own module, its re-export and its own
test. Every adapter here raised a raw `botocore.exceptions.ClientError` at callers who, by the
package's own contract, cannot name that type — which is how a missing bucket reached the lakehouse
storage browser as "Storage service unreachable (HTTP 500)".

PS-04 — the lazy-client/pickle trio (`@property client` + `__getstate__` + `__setstate__`, with the
same "Unreachable via __init__" comment) was duplicated byte-for-byte across `S3Source` and `S3Sink`,
and the `list_objects_v2` paginate loop was written three times in one file — with no `Source`/`Sink`
Protocol anywhere, so nothing said what an adapter even is. `build_source`/`build_sink` returned
`Any`, which is the shape of "the seam is undescribed".
"""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from moto import mock_aws

from storage import (
    BucketNotFoundError,
    FSSink,
    FSSource,
    ObjectNotFoundError,
    S3Sink,
    S3Source,
    Sink,
    Source,
    build_sink,
    build_source,
    iter_keys,
)


if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client


def _client():
    import boto3

    return boto3.client("s3", region_name="us-east-1")


def _make_s3_client():
    """Module-level factory for the pickling test."""
    return _client()


# ── PS-02: storage's own taxonomy, applied in storage ────────────────────────────────────────


@mock_aws
def test_reading_a_missing_key_raises_storages_own_error() -> None:
    client = _client()
    client.create_bucket(Bucket="real")
    source = S3Source(bucket="real", client=client)
    with pytest.raises(ObjectNotFoundError) as caught:
        source.read("nope.jpg")
    assert caught.value.bucket == "real"
    assert caught.value.key == "nope.jpg"


@mock_aws
def test_listing_a_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        list(S3Source(bucket="ghost", client=_client()).keys())


@mock_aws
def test_iter_keys_over_a_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        list(iter_keys(_client(), "ghost"))


@mock_aws
def test_writing_into_a_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        S3Sink(bucket="ghost", client=_client()).write("out/a.xml", b"<alto/>")


@mock_aws
def test_listing_a_sinks_missing_bucket_raises_storages_own_error() -> None:
    with pytest.raises(BucketNotFoundError):
        list(S3Sink(bucket="ghost", client=_client()).existing_keys())


@mock_aws
def test_a_credential_failure_is_not_laundered_into_a_not_found() -> None:
    """The taxonomy translates not-founds and NOTHING else — an outage deserves a 5xx, not a 404."""
    from botocore.exceptions import ClientError

    class _Denied:
        def get_object(self, **kwargs: object) -> dict:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")

    # cast, not an ignore: a stub that answers one method is the test's own claim about what
    # `read` touches, and boto3 ships no runtime type to build a real client against here.
    with pytest.raises(ClientError):
        S3Source(bucket="real", client=cast("S3Client", _Denied())).read("k")


# ── PS-04: one mechanism each ────────────────────────────────────────────────────────────────


def test_the_adapters_satisfy_the_source_and_sink_protocols(tmp_path: Path) -> None:
    assert isinstance(FSSource(root=tmp_path), Source)
    assert isinstance(FSSink(root=tmp_path), Sink)
    assert isinstance(S3Source(bucket="b", client_factory=_make_s3_client), Source)
    assert isinstance(S3Sink(bucket="b", client_factory=_make_s3_client), Sink)


def test_the_factories_are_typed_by_the_protocols(tmp_path: Path) -> None:
    """`build_source`/`build_sink` returned `Any` — the seam had no stated shape at all."""
    from typing import get_type_hints

    assert get_type_hints(build_source)["return"] is Source
    assert get_type_hints(build_sink)["return"] is Sink
    assert isinstance(build_source(str(tmp_path)), Source)
    assert isinstance(build_sink(str(tmp_path)), Sink)


def test_the_lazy_client_dance_is_written_once() -> None:
    assert S3Source.client is S3Sink.client, "the @property/__getstate__/__setstate__ trio is duplicated per adapter"
    assert S3Source.__getstate__ is S3Sink.__getstate__
    assert S3Source.__setstate__ is S3Sink.__setstate__


def test_the_paginate_loop_is_written_once() -> None:
    src = (Path(__file__).resolve().parents[1] / "src" / "storage").rglob("*.py")
    occurrences = sum(text.count('get_paginator("list_objects_v2")') for text in (p.read_text(encoding="utf-8") for p in src))
    assert occurrences == 1, f"the list_objects_v2 paginate loop is written {occurrences} times"


# ── the behaviour the de-duplication must preserve ───────────────────────────────────────────


def test_an_adapter_still_needs_a_client_or_a_factory() -> None:
    with pytest.raises(ValueError, match="S3Source"):
        S3Source(bucket="b")
    with pytest.raises(ValueError, match="S3Sink"):
        S3Sink(bucket="b")


def test_a_factory_built_adapter_still_drops_its_client_on_pickle() -> None:
    for adapter in (S3Source(bucket="b", prefix="p/", client_factory=_make_s3_client), S3Sink(bucket="b", client_factory=_make_s3_client)):
        _ = adapter.client  # force the lazy build
        restored = pickle.loads(pickle.dumps(adapter))  # noqa: S301
        assert restored._client is None
        assert restored.bucket == "b"
