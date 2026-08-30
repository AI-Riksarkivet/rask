"""The object browser's 404s must name the BUCKET they are talking about (VS-22).

open_python-audit VS-22. `bucket` on these routes is a STORE NAME — a key into the catalog's
storage registry — and the real bucket only appears at the boto call, via `_registered_bucket`. But
`s3_errors(bucket=bucket)` was handed the STORE name, so `exc.bucket` carried it too, and
`_missing_bucket` then told the operator:

    bucket not found: <store name> — the S3 backend has no such bucket. The platform provisions it
    from the chart's rustfs.buckets; check that the object store actually created it.

That sentence is about `rustfs.buckets`, which lists BUCKETS. An operator handed a store name goes
looking for a bucket nobody ever asked the chart to create, on a route whose whole reason to exist
is diagnosing an unprovisioned store. The names coincide for the shipped defaults, which is exactly
why it survived: it only misleads where it matters, on a store that renames its bucket.

The store name is not dropped — the caller addressed a store and the answer must still make sense to
them — so the object-level 404 keeps naming the store, while the bucket-level one names the bucket.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from service_kit.exceptions import NotFoundError
from service_kit.schemas.storage import StorageRole, Store
from storage import BucketNotFoundError, ObjectNotFoundError
from viewer.api.v1.endpoints import objects as objects_ep


if TYPE_CHECKING:
    from viewer.core.config import ViewerSettings

#: A store whose NAME differs from its BUCKET — the case the defaults hide.
STORE = Store(name="raw-drop", bucket="images-batch", role=StorageRole.RAW)


class _Settings:
    fga_root_object = "system:rask"


async def _allow(**_kw: object) -> bool:
    return True


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects_ep, "store_by_name", lambda name: STORE if name == STORE.name else None)


class _NoBucket:
    """Every call reports the bucket as absent, the way `s3_errors` translates NoSuchBucket."""

    def list_objects_v2(self, **kw: object) -> dict[str, object]:
        raise self._absent(kw)

    def head_object(self, **kw: object) -> dict[str, object]:
        raise self._absent(kw)

    def get_object(self, **kw: object) -> dict[str, object]:
        raise self._absent(kw)

    def head_bucket(self, **kw: object) -> dict[str, object]:
        raise self._absent(kw)

    @staticmethod
    def _absent(kw: dict[str, object]) -> Exception:
        from botocore.exceptions import ClientError

        return ClientError({"Error": {"Code": "NoSuchBucket", "Message": "The specified bucket does not exist"}}, "GetObject")


def _settings() -> ViewerSettings:
    return cast("ViewerSettings", _Settings())


def test_the_listing_404_names_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects_ep, "_client_for", lambda _n: _NoBucket())
    with pytest.raises(NotFoundError) as caught:
        asyncio.run(objects_ep.list_objects(checker=_allow, subject="gina", settings=_settings(), bucket=STORE.name))
    assert STORE.bucket in str(caught.value), (
        f"the 404 said {str(caught.value)!r} — an operator checking rustfs.buckets for {STORE.name!r} will not find it; the bucket is {STORE.bucket!r}"
    )


def test_the_head_404_names_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects_ep, "_client_for", lambda _n: _NoBucket())
    with pytest.raises(NotFoundError) as caught:
        asyncio.run(objects_ep.head_object(checker=_allow, subject="gina", settings=_settings(), bucket=STORE.name, key="a.tif"))
    assert STORE.bucket in str(caught.value), str(caught.value)


def test_the_download_404_names_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(objects_ep, "_client_for", lambda _n: _NoBucket())
    with pytest.raises(NotFoundError) as caught:
        asyncio.run(objects_ep.download_object(checker=_allow, subject="gina", settings=_settings(), bucket=STORE.name, key="a.tif"))
    assert STORE.bucket in str(caught.value), str(caught.value)


def test_a_present_bucket_with_a_missing_key_still_answers_by_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller addressed a STORE; the key-level answer stays in the caller's vocabulary."""

    class _NoKey:
        def head_object(self, **_kw: object) -> dict[str, object]:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist"}}, "HeadObject")

        def head_bucket(self, **_kw: object) -> dict[str, object]:
            return {}

    monkeypatch.setattr(objects_ep, "_client_for", lambda _n: _NoKey())
    with pytest.raises(NotFoundError) as caught:
        asyncio.run(objects_ep.head_object(checker=_allow, subject="gina", settings=_settings(), bucket=STORE.name, key="a.tif"))
    detail = str(caught.value)
    assert "object not found" in detail and STORE.name in detail, detail


def test_the_bucket_probe_asks_about_the_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_bucket_missing` is the probe that decides which 404 above is right; it must HEAD the real bucket."""
    asked: list[object] = []

    class _Probe:
        def head_bucket(self, **kw: object) -> dict[str, object]:
            asked.append(kw.get("Bucket"))
            return {}

    objects_ep._bucket_missing(_Probe(), STORE.bucket)
    assert asked == [STORE.bucket], f"the probe asked S3 about {asked} instead of the real bucket {STORE.bucket!r}"


def test_the_error_taxonomy_still_carries_the_bucket() -> None:
    """Guard on the collaborators these tests lean on, so a storage-package rename shows up here."""
    assert BucketNotFoundError("b").bucket == "b"
    assert ObjectNotFoundError(bucket="b", key="k").key == "k"
