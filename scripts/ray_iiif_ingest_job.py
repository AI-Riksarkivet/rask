"""Self-contained Ray IIIF→bronze harvest job — the P7a producer's Ray branch (re-tiered by R23).

The medallion producer submits this via the Ray Jobs REST API (``ray_submit.submit_iiif_ingest_job``) in
response to ``POST /ingest-iiif``: fetch one volume's manifest from the IIIF Image API (external raw),
harvest every page image across Ray workers, and write the BRONZE blob-v2 page dataset (file format 2.2,
stable row ids, the ``stage`` stamp — bronze is the first governed tier) in ONE atomic overwrite commit on
the driver. The producer then measures the committed dataset and emits the ONE bronze-write OpenLineage
event itself — this job writes data, never lineage (the head owns the emit, so a crashed job can't leave
a phantom COMPLETE).

SELF-CONTAINED like ``ray_stage_job.py`` / ``ray_train_job.py``: no ``services/`` imports — the manifest
parse + URL shape below are drift-pinned to ``packages/storage/src/storage/iiif.py`` by
``tests/unit/test_iiif_ingest.py`` (the unit venv imports this module, so ``ray``/``lance`` stay lazy).

Env: VOLUME_ID BRONZE_URI  [IIIF_BASE_URL IIIF_QUERY_PARAMS MAX_PAGES]
     [S3_ENDPOINT S3_KEY S3_SECRET S3_REGION] — empty endpoint = local-path target (dev).
     [TRACEPARENT TRACESTATE OTEL_* RASK_LINEAGE_*] — trace + lineage config injected by the submitter.
"""

from __future__ import annotations

import os
import sys
from typing import Any


DEFAULT_IIIF_BASE = "https://iiifintern-ai.ra.se"  # == storage.iiif.DEFAULT_IIIF_BASE (drift-pinned)
DEFAULT_QUERY_PARAMS = "full/max/0/default.jpg"  # == storage.iiif.DEFAULT_QUERY_PARAMS (drift-pinned)


def manifest_url(volume_id: str, base_url: str = DEFAULT_IIIF_BASE) -> str:
    """The volume's IIIF manifest URL — mirror of ``storage.iiif.get_image_ids``'s shape (drift-pinned)."""
    return f"{base_url}/arkis!{volume_id}/manifest"


def parse_image_ids(manifest: dict[str, Any]) -> list[str]:
    """Sorted image ids from a manifest — mirror of ``storage.iiif.get_image_ids`` (drift-pinned)."""
    image_ids: list[str] = []
    for item in manifest.get("items", []):
        raw_id = item.get("id", "")
        if "!" in raw_id:
            image_ids.append(raw_id.split("!")[1][:14].upper())
    return sorted(image_ids)


def image_url(image_id: str, base_url: str = DEFAULT_IIIF_BASE, query_params: str = DEFAULT_QUERY_PARAMS) -> str:
    """The page's Image-API URL — mirror of ``storage.iiif.build_image_url`` (drift-pinned)."""
    return f"{base_url}/arkis!{image_id}/{query_params}"


def _storage_options() -> dict[str, str]:
    endpoint = os.environ.get("S3_ENDPOINT", "")
    if not endpoint:
        return {}
    return {
        "endpoint": endpoint,
        "access_key_id": os.environ.get("S3_KEY", ""),
        "secret_access_key": os.environ.get("S3_SECRET", ""),
        "region": os.environ.get("S3_REGION", "us-east-1"),
        "allow_http": "true",
        "virtual_hosted_style_request": "false",
    }


def _fetch(url: str, attempts: int = 3, base_delay: float = 1.0) -> bytes:
    """GET with retry on transient errors/5xx — mirror of ``storage.iiif.fetch_image`` (drift-pinned)."""
    import time

    import httpx

    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = httpx.get(url, timeout=30.0)
            resp.raise_for_status()
            return resp.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                if code != 429 and 400 <= code < 500:
                    raise
            last = exc
            if i < attempts - 1:
                time.sleep(base_delay * (2**i))
    assert last is not None
    raise last


def main() -> int:
    import json

    import httpx
    import lance
    import pyarrow as pa
    import ray
    from lance import blob_array, blob_field

    volume_id = os.environ["VOLUME_ID"]
    bronze_uri = os.environ["BRONZE_URI"]
    base_url = os.environ.get("IIIF_BASE_URL", DEFAULT_IIIF_BASE).rstrip("/")
    query_params = os.environ.get("IIIF_QUERY_PARAMS", DEFAULT_QUERY_PARAMS)
    max_pages_raw = os.environ.get("MAX_PAGES", "")
    max_pages = int(max_pages_raw) if max_pages_raw else None

    manifest = json.loads(httpx.get(manifest_url(volume_id, base_url), timeout=60.0).content)
    image_ids = parse_image_ids(manifest)
    if max_pages is not None:
        image_ids = image_ids[:max_pages]
    if not image_ids:
        print(f"manifest for {volume_id} yielded no pages; refusing to write an empty bronze dataset", file=sys.stderr)
        return 1

    ray.init()  # in-cluster: attaches to the running Ray; the submitter injected creds + trace env

    # Parallel harvest across Ray workers; page order is restored by sorting on the id below, so the
    # positional `id` column is reproducible (same manifest -> same rows) like the in-process path.
    fetched = ray.get([_harvest_page.remote(image_id, base_url, query_params) for image_id in image_ids])
    fetched.sort(key=lambda page: page[0])

    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            blob_field("payload"),
            pa.field("source_uri", pa.string()),
            pa.field("volume_id", pa.string()),
            pa.field("page_key", pa.string()),
            pa.field("stage", pa.string()),  # bronze gets the stage stamp AT INGEST (R23) — matches services ingest
        ]
    )
    batch = pa.record_batch(
        {
            "id": pa.array(range(len(fetched)), pa.int64()),
            "payload": blob_array([data for _, _, data in fetched]),
            "source_uri": pa.array([url for _, url, _ in fetched], pa.string()),
            "volume_id": pa.array([volume_id] * len(fetched), pa.string()),
            "page_key": pa.array([image_id for image_id, _, _ in fetched], pa.string()),
            "stage": pa.array(["bronze"] * len(fetched), pa.string()),
        },
        schema=schema,
    )
    dataset = lance.write_dataset(
        [batch],
        bronze_uri,
        schema=schema,
        mode="overwrite",
        storage_options=_storage_options() or None,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    print(f"wrote {dataset.count_rows()} pages of {volume_id} to {bronze_uri} (version {dataset.version})")
    return 0


try:  # `ray` is absent in the unit venv — the drift-pin test imports only the pure helpers above.
    import ray as _ray

    @_ray.remote
    def _harvest_page(image_id: str, base_url: str, query_params: str) -> tuple[str, str, bytes]:
        url = image_url(image_id, base_url, query_params)
        return image_id, url, _fetch(url)
except ModuleNotFoundError:  # pragma: no cover — only taken outside the Ray image
    pass


if __name__ == "__main__":
    sys.exit(main())
