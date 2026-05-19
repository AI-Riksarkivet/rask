"""FTS-search support for the viewer.

Opens the Lance dataset at `s3://images-batch-search/lines.lance` and exposes
two operations:

    search(query, limit)              -> list[hit dict]
    fetch_thumb(batch, page, line_id) -> jpeg bytes  (proxy for the browser)

The dataset handle is cached at module level on first use. Lance opens are
cheap (one HEAD request to fetch the manifest) but we don't want one per
request. Re-opens itself if the manifest is replaced (e.g. after a fresh
indexer run) by re-checking on each query — Lance's `dataset()` is fast
enough for that.
"""

import logging
import os
from functools import lru_cache
from typing import Any

import boto3
import lance
from botocore.config import Config


logger = logging.getLogger(__name__)


SEARCH_BUCKET = os.environ.get("RASK_SEARCH_BUCKET", "images-batch-search")
LANCE_URI = f"s3://{SEARCH_BUCKET}/lines.lance"
# Riksarkivet EAD catalog — written by scripts/index_catalog.py via LanceDB,
# but the on-disk format is a plain Lance dataset so we open it the same way.
CATALOG_LANCE_URI = f"s3://{SEARCH_BUCKET}/archive_catalog.lance"


def _storage_options() -> dict[str, str]:
    """object_store-style options used by Lance for HCP/MinIO."""
    endpoint = os.environ["HCP_ENDPOINT"]
    return {
        "aws_endpoint": endpoint,
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.environ.get("AWS_REGION", "us-east-1"),
        "aws_virtual_hosted_style_request": "false",
        "allow_http": "true" if endpoint.startswith("http://") else "false",
    }


@lru_cache(maxsize=1)
def _s3() -> object:
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("HCP_ENDPOINT"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _open_dataset() -> lance.LanceDataset | None:
    """Open the Lance dataset (or return None if it's not been created yet)."""
    try:
        return lance.dataset(LANCE_URI, storage_options=_storage_options())
    except Exception as exc:
        logger.warning("could not open Lance dataset at %s: %s", LANCE_URI, exc)
        return None


def search(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Run an FTS query against the `text` column.

    Returns one dict per hit with the columns the frontend needs to render
    the result + jump to the viewer.
    """
    if not query.strip():
        return []
    ds = _open_dataset()
    if ds is None:
        return []
    cols = [
        "batch_id",
        "page_id",
        "page_idx",
        "line_id",
        "line_idx",
        "text",
        "confidence",
        "hpos",
        "vpos",
        "width",
        "height",
        "polygon",
        "thumb_key",
    ]
    tbl = ds.scanner(
        full_text_query={"query": query, "columns": ["text"]},
        columns=cols,
        limit=limit,
    ).to_table()
    out: list[dict[str, Any]] = []
    for row in tbl.to_pylist():
        # Map S3 thumb_key to a proxy URL the frontend can hit.
        if row.get("thumb_key"):
            row["thumb_url"] = f"/api/search/thumb/{row['thumb_key']}"
        else:
            row["thumb_url"] = None
        out.append(row)
    # Enrich every hit with its parent volume's catalog row so the search UI
    # can show fonds/date context next to the line text. One bulk lookup over
    # the unique batch_ids (== bild_ids) — typical query touches < 50 volumes.
    batch_ids = list({r["batch_id"] for r in out if r.get("batch_id")})
    catalog_rows = catalog_by_bild_ids(batch_ids)
    for r in out:
        r["catalog"] = catalog_rows.get(r.get("batch_id"))
    return out


def fetch_thumb(thumb_key: str) -> bytes | None:
    """GET a line thumbnail from the search bucket. Returns bytes or None on miss."""
    if not thumb_key.startswith("thumbs/"):
        # Defense in depth — keep the proxy from being a generic S3 fetch.
        return None
    try:
        resp = _s3().get_object(Bucket=SEARCH_BUCKET, Key=thumb_key)
        return resp["Body"].read()
    except Exception as exc:
        logger.warning("thumb GET %s/%s failed: %s", SEARCH_BUCKET, thumb_key, exc)
        return None


def stats() -> dict[str, Any]:
    """Lightweight `/api/search/stats` payload — surfaces dataset health."""
    ds = _open_dataset()
    if ds is None:
        return {"available": False, "rows": 0}
    return {"available": True, "rows": ds.count_rows()}


def _open_catalog_dataset() -> lance.LanceDataset | None:
    """Open the EAD catalog Lance dataset (or None if it's not been created yet)."""
    try:
        return lance.dataset(CATALOG_LANCE_URI, storage_options=_storage_options())
    except Exception as exc:
        logger.warning("could not open Lance dataset at %s: %s", CATALOG_LANCE_URI, exc)
        return None


def catalog_search(query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Run an FTS query against the catalog's `search_text` column.

    Returns one dict per hit with the columns the frontend needs to show the
    archive hierarchy, dates, snippet, and a link out to the public viewer
    (bildvisning_url) or IIIF manifest. The `vector` and `search_text` columns
    are deliberately omitted — the former is unused (we ingest with --no-embed)
    and the latter is just the FTS source.
    """
    if not query.strip():
        return []
    ds = _open_catalog_dataset()
    if ds is None:
        return []
    cols = [
        "id",
        "reference_code",
        "archive_code",
        "fonds_id",
        "fonds_title",
        "series_id",
        "series_title",
        "volume_id",
        "volume_title",
        "date_text",
        "date_start",
        "date_end",
        "description",
        # `bild_id` is the join key into batches.db (== batch_id locally) so
        # the viewer can decide whether to open the volume in our cache or
        # send the user out to Riksarkivet's public viewer.
        "bild_id",
        "bildvisning_url",
        "iiif_manifest",
        "thumbnail_url",
    ]
    tbl = ds.scanner(
        full_text_query={"query": query, "columns": ["search_text"]},
        columns=cols,
        limit=limit,
    ).to_table()
    return tbl.to_pylist()


def catalog_stats() -> dict[str, Any]:
    """`/api/catalog/search/stats` payload — mirrors stats() for the lines dataset."""
    ds = _open_catalog_dataset()
    if ds is None:
        return {"available": False, "rows": 0}
    return {"available": True, "rows": ds.count_rows()}


def catalog_by_bild_ids(bild_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Bulk-lookup `bild_id -> catalog row` for many ids in one Lance scan.

    Used by the Browse page — we have ~200-2000 cached/transcribed batch_ids
    to enrich, and N separate `bild_id = '<x>'` scans is wasteful. One
    `bild_id IN (...)` scan is much cheaper.

    Bild_ids missing from the catalog (e.g. test batches not in OAI-PMH)
    are simply absent from the returned dict.
    """
    if not bild_ids:
        return {}
    ds = _open_catalog_dataset()
    if ds is None:
        return {}
    cols = [
        "id",
        "reference_code",
        "archive_code",
        "fonds_id",
        "fonds_title",
        "series_id",
        "series_title",
        "volume_id",
        "volume_title",
        "date_text",
        "date_start",
        "date_end",
        "description",
        "bild_id",
        "bildvisning_url",
        "iiif_manifest",
        "thumbnail_url",
    ]
    quoted = ",".join("'" + bid.replace("'", "''") + "'" for bid in bild_ids)
    tbl = ds.scanner(
        filter=f"bild_id IN ({quoted})",
        columns=cols,
    ).to_table()
    return {r["bild_id"]: r for r in tbl.to_pylist()}


def catalog_by_bild_id(bild_id: str) -> dict[str, Any] | None:
    """Direct catalog lookup by bild_id (== our batch_id). Returns None if absent.

    Used by the viewer's metadata panel — every batch_id we have a viewer
    page for resolves through here, so a fast filtered scan is the right
    primitive (no FTS needed). The bild_id column is dense and unique-ish
    so the filter is cheap even without a dedicated index.
    """
    if not bild_id:
        return None
    ds = _open_catalog_dataset()
    if ds is None:
        return None
    cols = [
        "id",
        "reference_code",
        "archive_code",
        "fonds_id",
        "fonds_title",
        "series_id",
        "series_title",
        "volume_id",
        "volume_title",
        "date_text",
        "date_start",
        "date_end",
        "description",
        "bild_id",
        "bildvisning_url",
        "iiif_manifest",
        "thumbnail_url",
    ]
    # Filter on bild_id; expect 0 or 1 row but `limit=1` cheap-guards either way.
    safe_id = bild_id.replace("'", "''")
    tbl = ds.scanner(
        filter=f"bild_id = '{safe_id}'",
        columns=cols,
        limit=1,
    ).to_table()
    rows = tbl.to_pylist()
    return rows[0] if rows else None
