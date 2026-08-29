"""S3 object browser — the lakehouse storage browser's backend (R18).

Ported from the retired rask ``volumes-api`` in the R6/R20 media wave
(docs/architecture/lance-ns-merge.md): one delimiter-scoped listing, a HEAD,
and a STREAMED byte download over any bucket in the catalog's storage registry
(it was two fixed buckets once — that premise died with `LANCE_STORES`, and it
is why the download streams rather than buffers). Public paths ride the
existing ``/api/explorer`` gateway row (``/api/explorer/objects`` → ``/api/objects``
here), so the gateway grows zero new rows.

Uses ``storage.s3_client`` (never raw boto3); endpoint/creds resolve from env
(``RASK_S3_ENDPOINT_URL`` / ``AWS_*``). Routes are ``async def`` for the awaited
FGA prologue; every blocking boto3 body then runs via ``run_in_threadpool`` — it
must never run inline on the loop (open_python-audit VS-01: this docstring
claimed sync ``def`` while all three routes were coroutines doing boto3 inline).

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
from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Query
from fastapi.concurrency import iterate_in_threadpool, run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from service_kit.exceptions import ForbiddenError, NotFoundError, ServiceUnavailableError
from service_kit.governed.audit import FAILURE, audit
from service_kit.governed.secrets import fetch_dapr_secret
from service_kit.schemas.storage import Store, store_by_name
from storage import BucketNotFoundError, ObjectNotFoundError, s3_client, s3_errors
from viewer.api.security import BROWSE_STORAGE, CheckerDep, CurrentSubject, SettingsDep
from viewer.core.config import get_viewer_settings


log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["objects"])

#: Bytes pulled from S3 per `download_object` step. 1 MiB amortizes the per-read overhead while
#: bounding what one in-flight download holds — the same trade `media.py` makes for blob ranges.
_DOWNLOAD_CHUNK = 1 << 20


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

    No try/except around the fetch: `fetch_dapr_secret` swallows every failure internally and returns
    `{}` — a down sidecar, a missing secret and an empty secret all look the same here (VS-11: a dead
    handler "caught" the fetch while the real miss fell through to a message claiming the secret
    existed). The empty/incomplete bundle is the ONLY failure signal, so the 503 says the credentials
    could not be read and asserts nothing about which of the three states caused it.
    """
    store = get_viewer_settings().secret_store
    data = fetch_dapr_secret(store, secret, retries=1)
    ak, sk = data.get("access_key"), data.get("secret_key")
    if not (ak and sk):
        log.warning("store_secret_unavailable", extra={"secret": secret})
        raise ServiceUnavailableError(
            f"credentials for this store could not be read: secret {secret!r} from the {store!r} secret "
            "store yielded no access_key/secret_key pair — the store may be unreachable, the secret "
            "missing, or seeded without the pair. The store is not readable until it resolves."
        )
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
    next_continuation_token: str | None = None
    """S3's own opaque cursor, handed straight back.

    The route used to consume this internally and expose nothing, which is the "unbounded fetch-all"
    `pagination.md` exists to prevent: the caller could ask neither for less nor for more. The token is
    already opaque, so there is no cursor to invent — only one to stop swallowing. `None` means this
    was the last page."""


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


def _resolve_missing(client: object, exc: ObjectNotFoundError, *, store: str) -> NotFoundError:
    """Turn a key-level not-found into the RIGHT 404, on any S3 backend.

    A `HEAD` has no response body, so some backends answer a missing bucket with a bare
    `404` that is indistinguishable from a missing key (moto and MinIO volunteer
    `NoSuchBucket`; AWS does not). Rather than let the answer depend on which store is
    plugged in — the storage-agnostic contract forbids exactly that — probe the bucket
    once, on the error path only, and say which thing is actually absent.

    `exc.bucket` is the real bucket (that is what the boto call named), while `store` is what the
    CALLER addressed. The key-level answer uses the caller's vocabulary and the bucket-level one
    uses the operator's — VS-22: both used to be the store name, so the bucket-not-found 404 sent an
    operator to look for a `rustfs.buckets` entry that had never been named that.
    """
    return _missing_bucket(exc.bucket) if _bucket_missing(client, exc.bucket) else NotFoundError(f"object not found: {store}/{exc.key}")


def _bucket_missing(client: object, bucket: str) -> bool:
    """True only when the store positively reports the bucket as absent. ``bucket`` is the REAL
    bucket, not the store name — this call goes straight to S3.

    A probe that fails for any OTHER reason returns False: an inconclusive probe must
    never manufacture a "your chart did not provision this" claim.
    """
    head_bucket = getattr(client, "head_bucket", None)
    if head_bucket is None:
        return False
    try:
        with s3_errors(bucket=bucket):
            head_bucket(Bucket=bucket)
    except BucketNotFoundError:
        return True
    except Exception:  # noqa: BLE001 — an inconclusive probe is not evidence; fall back to the key answer
        return False
    return False


async def _require_browse(checker: object, subject: str, settings: object, store: str, action: str) -> None:
    """Estate-admin gate for the raw object routes (#90).

    These three routes — an S3 LIST, a HEAD, and a FULL BYTE DOWNLOAD over any bucket in the store
    registry — shipped with no token dependency and no authorization at all. They validated only the
    store NAME against the registry, which answers "is this a bucket we know" and never "may you read
    it". Their neighbour `datasets.py` was already FGA-gated, so this was an inconsistency rather
    than a stance.

    Checked against the ROOT object, not the store. The relation resolves on any warehouse its owner
    owns, so what makes this an estate privilege is precisely that this call names `fga_root_object`
    — the same arrangement `can_observe_events` has. Checking it against a tenant warehouse would
    silently widen the scope, which is why the object is built here and not passed in.
    """
    obj = settings.fga_root_object  # ty: ignore[unresolved-attribute]
    if not await checker(user=subject, relation=BROWSE_STORAGE, obj=obj):  # ty: ignore[call-non-callable]
        audit(action, FAILURE, subject=subject, resource=store, relation=BROWSE_STORAGE)
        raise ForbiddenError(f"{subject} lacks {BROWSE_STORAGE} on {obj}")


@router.get("/objects")
async def list_objects(
    checker: CheckerDep,
    subject: CurrentSubject,
    settings: SettingsDep,
    bucket: BucketName,
    prefix: Annotated[str, Query(description='Key prefix to list under (delimiter "/").')] = "",
    # S3's own per-call ceiling is 1000 keys, so `le` matches the protocol rather than inventing a
    # number. Every growth driver under a flat prefix here is monotonic — one object per Lance
    # fragment, one manifest per commit, one transaction per commit, and the cascade commits per stage
    # per run — so "one level" is not a bound.
    max_keys: Annotated[int, Query(ge=1, le=1000, description="Maximum keys per page (S3 caps at 1000).")] = 1000,
    continuation_token: Annotated[str | None, Query(description="Opaque cursor from a previous page's next_continuation_token.")] = None,
) -> S3Listing:
    """List one delimiter-scoped level of `bucket`/`prefix` for the storage browser.

    404s (never 500s) when the bucket itself is absent — see the module docstring.
    """
    await _require_browse(checker, subject, settings, bucket, "viewer.objects.list")

    def _blocking() -> S3Listing:
        client = _client_for(bucket)
        prefixes: list[str] = []
        objects: list[S3Object] = []
        # ONE call, not a drained paginator. The paginator was lazy — the first HTTP call, and so
        # NoSuchBucket, happened on ITERATION — and it iterated to exhaustion, which is the defect:
        # slicing a fully-drained paginator would have satisfied a `max_keys` parameter while still
        # reading the whole prefix into memory. Still inside `s3_errors` for the same translation.
        #
        # The REAL bucket, resolved once and used for both the call and the error translation: a
        # `s3_errors(bucket=<store name>)` makes `exc.bucket` a store name, and the 404 it feeds
        # then talks about a bucket nobody registered under that name (VS-22).
        real_bucket = _registered_bucket(bucket)
        request: dict[str, object] = {
            "Bucket": real_bucket,
            "Prefix": prefix,
            "Delimiter": "/",
            "MaxKeys": max_keys,
        }
        if continuation_token:
            request["ContinuationToken"] = continuation_token
        with s3_errors(bucket=real_bucket):
            page = client.list_objects_v2(**request)
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
        return S3Listing(
            bucket=bucket,
            prefix=prefix,
            prefixes=prefixes,
            objects=objects,
            next_continuation_token=page.get("NextContinuationToken"),
        )

    try:
        return await run_in_threadpool(_blocking)
    except BucketNotFoundError as exc:
        raise _missing_bucket(exc.bucket) from exc


@router.get("/object")
async def head_object(
    checker: CheckerDep,
    subject: CurrentSubject,
    settings: SettingsDep,
    bucket: BucketName,
    key: Annotated[str, Query(description="Full object key to describe.")],
) -> S3ObjectHead:
    """Metadata (size/content-type/last-modified/etag) for a single object.

    404 for a missing key AND for a missing bucket — with different details, so the
    answer says which. Anything else (endpoint down, bad credentials) propagates: the
    old blanket `except Exception` reported an outage as "object not found", which is
    the same lie in the other direction.
    """
    await _require_browse(checker, subject, settings, bucket, "viewer.object.head")

    def _blocking() -> dict:
        client = _client_for(bucket)
        real_bucket = _registered_bucket(bucket)
        try:
            with s3_errors(bucket=real_bucket, key=key):
                return client.head_object(Bucket=real_bucket, Key=key)
        except BucketNotFoundError as exc:
            raise _missing_bucket(exc.bucket) from exc
        except ObjectNotFoundError as exc:
            raise _resolve_missing(client, exc, store=bucket) from exc

    resp = await run_in_threadpool(_blocking)
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
async def download_object(
    checker: CheckerDep,
    subject: CurrentSubject,
    settings: SettingsDep,
    bucket: BucketName,
    key: Annotated[str, Query(description="Full object key to download.")],
) -> StreamingResponse:
    """The object's bytes with a download disposition (404 if missing).

    STREAMED, in `_DOWNLOAD_CHUNK` pieces (VS-10 is the disposition; VS-15 is this). It used to
    `read()` the whole object into the process before sending a byte, defended by a premise that had
    already been deleted: "the two rask buckets hold page images (~MBs) and ALTO XML (small)". The
    bucket list became CONFIGURATION (`store_by_name` over the registry, `DEFAULT_STORES`), and the
    registered stores include the warehouse and the observability bucket — multi-GB objects, one
    full copy per concurrent request, on a route with no size bound. Bounded memory is not a
    micro-optimisation here; it is the difference between a large download and an OOM.

    Doubles as the browser's inline `<img src>` — a disposition header never stops an `<img>` fetch
    from rendering, and a streamed body is still a normal 200 with a `Content-Length`.

    Same 404 split as the HEAD sibling (missing key vs missing bucket), and it still happens BEFORE
    the response starts: the GET is issued in the threadpool and only its BODY is deferred, so an
    absence is a 404 rather than an error rendered mid-download. Outages still surface as outages.
    """
    await _require_browse(checker, subject, settings, bucket, "viewer.object.download")

    def _blocking() -> tuple[Any, dict]:
        # The client is RETURNED, not discarded: the streaming body borrows its connection pool, so
        # letting the client fall out of scope here would break the transfer mid-flight.
        client = _client_for(bucket)
        real_bucket = _registered_bucket(bucket)
        try:
            with s3_errors(bucket=real_bucket, key=key):
                return client, client.get_object(Bucket=real_bucket, Key=key)
        except BucketNotFoundError as exc:
            raise _missing_bucket(exc.bucket) from exc
        except ObjectNotFoundError as exc:
            raise _resolve_missing(client, exc, store=bucket) from exc

    client, resp = await run_in_threadpool(_blocking)
    body = resp["Body"]

    def _chunks(_client: Any = client) -> Iterator[bytes]:  # noqa: ANN401 — the boto3 client, bound to keep its
        # connection pool alive for as long as the body is being read; see `_blocking`.
        try:
            yield from body.iter_chunks(_DOWNLOAD_CHUNK)
        finally:
            body.close()

    headers = {"Content-Disposition": _attachment_disposition(key.rsplit("/", 1)[-1] or "download")}
    length = resp.get("ContentLength")
    if length is not None:
        headers["Content-Length"] = str(length)
    return StreamingResponse(
        # `iterate_in_threadpool`, because every `iter_chunks` pull is a blocking socket read and
        # this handler is async: iterating the generator inline would read S3 on the event loop.
        iterate_in_threadpool(_chunks()),
        media_type=resp.get("ContentType") or "application/octet-stream",
        headers=headers,
    )


def _attachment_disposition(filename: str) -> str:
    """A Content-Disposition the caller-supplied key cannot break out of (VS-10).

    The key is a query parameter, so its basename is attacker-shaped: a `"` inside the quoted
    `filename=` ends the quoted-string early and the remainder parses as extra disposition
    parameters (filename spoofing). The ASCII fallback therefore drops `"`/`\\` and anything
    non-printable; the REAL name is not lost — it rides the RFC 6266 `filename*=UTF-8''` ext
    parameter, pct-encoded, which conforming clients prefer over the fallback.
    """
    fallback = "".join(c for c in filename if c.isascii() and c.isprintable() and c not in '"\\') or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"
