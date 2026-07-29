"""S3 object browser — the lakehouse storage browser's backend (R18).

Ported from the retired rask ``volumes-api`` in the R6/R20 media wave
(docs/architecture/lance-ns-merge.md): one delimiter-scoped listing, a HEAD,
and a byte download over the two fixed rask buckets. Public paths ride the
existing ``/api/media`` gateway row (``/api/media/objects`` → ``/api/objects``
here), so the gateway grows zero new rows.

Uses ``storage.s3_client`` (never raw boto3); endpoint/creds resolve from env
(``RASK_S3_ENDPOINT_URL`` / ``AWS_*``). Routes are sync ``def`` — FastAPI runs
the blocking boto calls in its threadpool.

**Failure posture (live-proof 2026-07-28, defect 2).** A bucket that does not exist
on the S3 backend is an EXPECTED, diagnosable state — the chart provisions
``rustfs.buckets`` through an operator Tenant, and a blocked Tenant leaves them
absent. It used to surface as an unhandled ``botocore`` ``NoSuchBucket`` → HTTP 500
→ the storage browser's "Storage service unreachable", which named neither the
bucket nor the cause. Every route below now translates the S3 boundary through
``storage.s3_errors`` and answers **404 with the bucket (and key) in the detail**;
only a genuine outage — unreachable endpoint, bad credentials — still reaches the
500 path, which is what a 500 should mean.
"""

import logging
import os
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel

from service_kit.exceptions import NotFoundError, ServiceUnavailableError
from service_kit.governed.secrets import fetch_dapr_secret
from service_kit.schemas.storage import Store, store_by_name
from storage import BucketNotFoundError, ObjectNotFoundError, s3_client, s3_errors


log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["objects"])


#: R28: the bucket a request may name is validated against the CATALOG'S STORAGE REGISTRY, not a
#: Literal union. It used to be ``Literal["images-batch", "images-batch-alto"]``, hand-mirrored into
#: the lakehouse zone's storage.ts — two copies of one fact in two languages, kept in step by
#: discipline, and neither saying what either bucket was FOR. Registering a store is now config
#: (``LANCE_STORES``); this endpoint and the UI validate against the SAME list, so they cannot
#: disagree about which stores exist.
def _registered_store(name: str) -> Store:
    """Resolve a store NAME to its registry entry, or 404 naming the store.

    A 404 rather than a 422: an unregistered store is a missing resource, not a malformed request,
    and the distinction matters to a browser that lists stores from the registry — if it asks for
    one, the registry said it existed.
    """
    store = store_by_name(name)
    if store is None:
        raise NotFoundError(f"no registered store named {name!r}")
    return store


def _registered_bucket(name: str) -> str:
    """The bucket behind a store name. Kept for call sites that need only the bucket."""
    return _registered_store(name).bucket


@lru_cache(maxsize=32)
def _creds(secret: str) -> tuple[str, str]:
    """This store's credentials from the Dapr secret store. FAIL-CLOSED — never falls back to env.

    The secret store is the SOLE source for a store that declares one. An earlier version degraded to
    the process env when the lookup failed, and that is the cheat this docstring exists to forbid: env
    holds the WAREHOUSE's credentials, so a failed lookup for an external store silently retried it
    against the wrong backend — an InvalidAccessKeyId if you are lucky, and someone else's bucket if
    you are not. A secret store that is down must look down.

    Cached: a secret is estate config, not per-request data, and a sidecar round-trip per listing would
    put OpenBao on the object browser's hot path. Only SUCCESSFUL lookups cache — `lru_cache` never
    stores a raised exception, so a transient outage cannot pin a failure for the process lifetime.
    """
    store = os.getenv("RASK_SECRET_STORE", "lance-secrets")
    try:
        data = fetch_dapr_secret(store, secret, retries=1)
    except Exception as exc:
        log.warning("store_secret_unavailable", extra={"secret": secret})
        raise ServiceUnavailableError(
            f"credentials for this store are unavailable: secret {secret!r} could not be read from "
            f"the {store!r} secret store ({exc}). The store is not readable until it resolves."
        ) from exc
    ak, sk = data.get("access_key"), data.get("secret_key")
    if not (ak and sk):
        raise ServiceUnavailableError(f"secret {secret!r} exists but carries no access_key/secret_key pair")
    return (ak, sk)


def _client_for(name: str) -> Any:  # noqa: ANN401 — boto3 client, same as storage.s3_client
    """An S3 client for THIS store — its own endpoint, credentials and TLS posture.

    Every route used a bare `s3_client()`, which resolves all three from process env, so every store
    was read from the deployment's warehouse regardless of where it lives. The raw tier is external, so
    `images-batch` was queried against the warehouse, correctly answered "no such objects", and a
    bucket holding millions of objects rendered as empty. Nothing errored — the listing was silently
    wrong, which is the least debuggable failure available.

    All three fields are optional and default to the env chain, so the governed tiers behave exactly
    as before.
    """
    store = _registered_store(name)
    # A store that declares a secret gets ONLY that secret's credentials; one that declares none uses
    # the deployment's own env. There is no third path — no falling back from the first to the second.
    ak, sk = _creds(store.secret) if store.secret else (None, None)
    return s3_client(store.endpoint, access_key=ak, secret_key=sk, insecure=store.insecure or None)


#: The dependency every route uses in place of the deleted union.
BucketName = Annotated[
    str,
    Query(description="A store registered in the catalog's storage registry (GET /v1/stores)."),
]


class S3Object(BaseModel):
    """A single object under a prefix (mirrors `S3Object` in the zone's storage.ts)."""

    key: str
    size: int
    last_modified: str | None


class S3Listing(BaseModel):
    """A delimiter-listed level of a bucket (mirrors `S3Listing` in storage.ts).

    `prefixes` are the "folder" common-prefixes directly under `prefix`; `objects`
    are the leaf keys at this level.
    """

    bucket: str
    prefix: str
    prefixes: list[str]
    objects: list[S3Object]


class S3ObjectHead(BaseModel):
    """Metadata for a single object (S3 HEAD) — the browser's preview panel.

    Mirrors `S3ObjectHead` in the zone's storage.ts.
    """

    key: str
    size: int
    content_type: str | None
    last_modified: str | None
    etag: str | None


def _missing_bucket(bucket: str) -> NotFoundError:
    """A 404 that NAMES the bucket and says who is supposed to have created it.

    An empty listing would have been the other honest option, and it is the wrong one:
    "bucket absent" and "bucket empty" would then read identically, which is exactly how
    an unprovisioned store stayed invisible until someone read a traceback.
    """
    return NotFoundError(
        f"bucket not found: {bucket} — the S3 backend has no such bucket. "
        "The platform provisions it from the chart's rustfs.buckets; check that the "
        "object store actually created it."
    )


def _resolve_missing(client: object, exc: ObjectNotFoundError) -> NotFoundError:
    """Turn a key-level not-found into the RIGHT 404, on any S3 backend.

    A `HEAD` has no response body, so some backends answer a missing bucket with a bare
    `404` that is indistinguishable from a missing key (moto and MinIO volunteer
    `NoSuchBucket`; AWS does not). Rather than let the answer depend on which store is
    plugged in — the storage-agnostic contract forbids exactly that — probe the bucket
    once, on the error path only, and say which thing is actually absent.
    """
    return _missing_bucket(exc.bucket) if _bucket_missing(client, exc.bucket) else NotFoundError(f"object not found: {exc.bucket}/{exc.key}")


def _bucket_missing(client: object, bucket: str) -> bool:
    """True only when the store positively reports the bucket as absent.

    A probe that fails for any OTHER reason returns False: an inconclusive probe must
    never manufacture a "your chart did not provision this" claim.
    """
    head_bucket = getattr(client, "head_bucket", None)
    if head_bucket is None:
        return False
    try:
        with s3_errors(bucket=bucket):
            head_bucket(Bucket=_registered_bucket(bucket))
    except BucketNotFoundError:
        return True
    except Exception:  # noqa: BLE001 — an inconclusive probe is not evidence; fall back to the key answer
        return False
    return False


@router.get("/objects")
def list_objects(
    bucket: BucketName,
    prefix: Annotated[str, Query(description='Key prefix to list under (delimiter "/").')] = "",
) -> S3Listing:
    """List one delimiter-scoped level of `bucket`/`prefix` for the storage browser.

    404s (never 500s) when the bucket itself is absent — see the module docstring.
    """
    client = _client_for(bucket)
    paginator = client.get_paginator("list_objects_v2")
    prefixes: list[str] = []
    objects: list[S3Object] = []
    try:
        # The pagination itself is inside the block: boto3's paginator is LAZY, so the
        # first HTTP call — and therefore NoSuchBucket — happens on iteration, not on
        # `paginate(...)`. Wrapping only the call would translate nothing.
        with s3_errors(bucket=bucket):
            for page in paginator.paginate(Bucket=_registered_bucket(bucket), Prefix=prefix, Delimiter="/"):
                prefixes.extend(cp["Prefix"] for cp in page.get("CommonPrefixes", []))
                for obj in page.get("Contents", []):
                    # Skip the prefix's own placeholder key (the "folder" marker).
                    if obj["Key"] == prefix:
                        continue
                    last_modified = obj.get("LastModified")
                    objects.append(
                        S3Object(
                            key=obj["Key"],
                            size=obj["Size"],
                            last_modified=last_modified.isoformat() if last_modified is not None else None,
                        )
                    )
    except BucketNotFoundError as exc:
        raise _missing_bucket(exc.bucket) from exc
    return S3Listing(bucket=bucket, prefix=prefix, prefixes=prefixes, objects=objects)


@router.get("/object")
def head_object(
    bucket: BucketName,
    key: Annotated[str, Query(description="Full object key to describe.")],
) -> S3ObjectHead:
    """Metadata (size/content-type/last-modified/etag) for a single object.

    404 for a missing key AND for a missing bucket — with different details, so the
    answer says which. Anything else (endpoint down, bad credentials) propagates: the
    old blanket `except Exception` reported an outage as "object not found", which is
    the same lie in the other direction.
    """
    client = _client_for(bucket)
    try:
        with s3_errors(bucket=bucket, key=key):
            resp = client.head_object(Bucket=_registered_bucket(bucket), Key=key)
    except BucketNotFoundError as exc:
        raise _missing_bucket(exc.bucket) from exc
    except ObjectNotFoundError as exc:
        raise _resolve_missing(client, exc) from exc
    last_modified = resp.get("LastModified")
    etag = (resp.get("ETag") or "").strip('"')
    return S3ObjectHead(
        key=key,
        size=resp["ContentLength"],
        content_type=resp.get("ContentType"),
        last_modified=last_modified.isoformat() if last_modified is not None else None,
        etag=etag or None,
    )


@router.get("/object/download")
def download_object(
    bucket: BucketName,
    key: Annotated[str, Query(description="Full object key to download.")],
) -> Response:
    """The object's bytes with a download disposition (404 if missing).

    Reads fully into memory: the two rask buckets hold page images (~MBs) and
    ALTO XML (small), so streaming isn't worth the boto3 client-lifetime juggling.
    Doubles as the browser's inline `<img src>` — a disposition header never stops
    an `<img>` fetch from rendering.

    Same 404 split as the HEAD sibling (missing key vs missing bucket); outages still
    surface as outages.
    """
    client = _client_for(bucket)
    try:
        with s3_errors(bucket=bucket, key=key):
            resp = client.get_object(Bucket=_registered_bucket(bucket), Key=key)
            data = resp["Body"].read()
    except BucketNotFoundError as exc:
        raise _missing_bucket(exc.bucket) from exc
    except ObjectNotFoundError as exc:
        raise _resolve_missing(client, exc) from exc
    content_type = resp.get("ContentType") or "application/octet-stream"
    filename = key.rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
