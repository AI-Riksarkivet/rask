"""Seed bronze$pages with REAL Riksarkivet page images, through the production writer.

Why this exists: the chart's producer deployment ships without MEDALLION_COMPUTE_ENABLED /
MEDALLION_IIIF_BRONZE_URI, so POST /ingest-iiif answers 409 ("iiif ingest head is not configured")
on a default install — there is no way to get bronze page data into a fresh estate from the API.
Until that is enabled by default, this seeds a volume so the document viewer has something REAL to
render.

It is NOT a fixture generator. It calls the same IIIFVolumeSource + ingest_to_bronze that the
/ingest-iiif route calls, so what it leaves behind is a genuine bronze blob-v2 page table.

Note the base URL: the chart defaults to https://iiifintern-ai.ra.se, which resolves only on RA's
network. This uses the PUBLIC endpoint so it works from anywhere.

Run against a forwarded RustFS:
    kubectl port-forward svc/rask-rustfs-io 9900:9000
    uv run python scripts/seed_bronze_pages.py

Not a fixture: this calls the same IIIFVolumeSource + ingest_to_bronze the producer's /ingest-iiif
route calls, so the dataset it leaves is a genuine bronze blob-v2 page table — the thing the
document viewer must be able to read.
"""
from medallion.services.iiif_produce import IIIFVolumeSource
from medallion.services.ingest import ingest_to_bronze

BASE = "https://lbiiif.riksarkivet.se"          # the PUBLIC endpoint (the chart defaults to RA-internal)
URI = "s3://lance-catalog/medallion/bronze-pages"
OPTS = {
    "aws_access_key_id": "rustfsadmin",
    "aws_secret_access_key": "rustfsadmin",
    "aws_endpoint": "http://127.0.0.1:9900",
    "aws_allow_http": "true",
    "aws_region": "us-east-1",
    "s3_express": "false",
}

src = IIIFVolumeSource("A0060198", base_url=BASE, query_params="full/max/0/default.jpg", timeout=60.0, max_pages=3)
res = ingest_to_bronze(src, URI, OPTS, max_objects=10, max_total_bytes=200 << 20)
print("ingested:", res)
