"""S3 object browser — the lakehouse storage browser's backend (R18).

Ported from the retired rask ``volumes-api`` in the R6/R20 media wave
(docs/architecture/lance-ns-merge.md): one delimiter-scoped listing, a HEAD,
and a byte download over the two fixed rask buckets. Public paths ride the
existing ``/api/media`` gateway row (``/api/media/objects`` → ``/api/objects``
here), so the gateway grows zero new rows.

Uses ``storage.s3_client`` (never raw boto3); endpoint/creds resolve from env
(``RASK_S3_ENDPOINT_URL`` / ``AWS_*``). Routes are sync ``def`` — FastAPI runs
the blocking boto calls in its threadpool.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel

from service_kit.media.exceptions import NotFoundError
from storage import s3_client


router = APIRouter(prefix="/api", tags=["objects"])

#: The two fixed rask buckets (input images + derived ALTO); mirrors `BUCKETS` in the
#: lakehouse zone's storage.ts. Generalizing to the warehouse's own buckets is a
#: recorded follow-up (docs/OPEN-WORK.md), not silently widened here.
Bucket = Literal["images-batch", "images-batch-alto"]


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


@router.get("/objects")
def list_objects(
    bucket: Annotated[Bucket, Query(description="One of the two fixed rask buckets.")],
    prefix: Annotated[str, Query(description='Key prefix to list under (delimiter "/").')] = "",
) -> S3Listing:
    """List one delimiter-scoped level of `bucket`/`prefix` for the storage browser."""
    client = s3_client()
    paginator = client.get_paginator("list_objects_v2")
    prefixes: list[str] = []
    objects: list[S3Object] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
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
    return S3Listing(bucket=bucket, prefix=prefix, prefixes=prefixes, objects=objects)


@router.get("/object")
def head_object(
    bucket: Annotated[Bucket, Query(description="One of the two fixed rask buckets.")],
    key: Annotated[str, Query(description="Full object key to describe.")],
) -> S3ObjectHead:
    """Metadata (size/content-type/last-modified/etag) for a single object (404 if missing)."""
    client = s3_client()
    try:
        resp = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        # Broad catch at the storage boundary (the rule is "no reach into
        # boto/botocore") — a missing key or any read failure is a 404.
        raise NotFoundError(f"object not found: {bucket}/{key}") from exc
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
    bucket: Annotated[Bucket, Query(description="One of the two fixed rask buckets.")],
    key: Annotated[str, Query(description="Full object key to download.")],
) -> Response:
    """The object's bytes with a download disposition (404 if missing).

    Reads fully into memory: the two rask buckets hold page images (~MBs) and
    ALTO XML (small), so streaming isn't worth the boto3 client-lifetime juggling.
    Doubles as the browser's inline `<img src>` — a disposition header never stops
    an `<img>` fetch from rendering.
    """
    client = s3_client()
    try:
        resp = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise NotFoundError(f"object not found: {bucket}/{key}") from exc
    data = resp["Body"].read()
    content_type = resp.get("ContentType") or "application/octet-stream"
    filename = key.rsplit("/", 1)[-1] or "download"
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
