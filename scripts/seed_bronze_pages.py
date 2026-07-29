"""Seed ``bronze$pages`` with REAL Riksarkivet page images — REGISTERED IN THE CATALOG.

Why this exists: the chart's producer ships without MEDALLION_COMPUTE_ENABLED /
MEDALLION_IIIF_BRONZE_URI, so ``POST /ingest-iiif`` answers 409 ("iiif ingest head is not
configured") on a default install. A fresh estate therefore has no way to get bronze page data in
from the API at all, and the document viewer has nothing to render.

TIERS ARE NAMESPACES. bronze/silver/gold are catalog NAMESPACES and the tables live inside them —
``bronze$pages``, ``bronze$events``, ``silver$features``, ``gold$catalog``, where ``$`` is
LANCE_NS_DELIMITER. This is the same shape Unity Catalog uses for schemas (``unity.bronze.raw_data``)
and Polaris for namespaces; rask has two levels where those have three, because the third — the
catalog/project — is carried per project as a warehouse.

An earlier version of this script wrote straight to a raw ``medallion/`` path under the bucket,
hyphen and all, bypassing the namespace entirely. The bytes landed and the viewer could read them by URI, but the catalog
knew nothing — no namespace, no table, no FGA ownership, no lineage. Namespaces and Tables both
rendered empty while the bucket held the data. That is ungoverned data inside a governed lakehouse,
which is the one thing the catalog exists to prevent.

So this now does BOTH halves, in order:
  1. write the dataset with the production writer (``ingest_to_bronze``), and
  2. register it through the CATALOG — ``POST /v1/namespace/bronze/create`` then
     ``POST /v1/table/bronze$pages/register`` — which is what seeds FGA ownership and emits the
     REGISTER_TABLE lineage edge. Registration is a separate step precisely because it is the
     governed one; a write that skips it is the bug this script used to have.

Run against a forwarded RustFS and a reachable catalog:

    kubectl port-forward svc/rask-rustfs-io 9900:9000 &
    kubectl port-forward svc/rask-catalog 2333:2333 &
    uv run python scripts/seed_bronze_pages.py
"""

from __future__ import annotations

import os
import sys

import httpx
from medallion.services.iiif_produce import IIIFVolumeSource
from medallion.services.ingest import ingest_to_bronze


#: The PUBLIC Riksarkivet IIIF endpoint. The chart defaults to https://iiifintern-ai.ra.se, which
#: resolves only on RA's network, so a developer anywhere else cannot harvest at all.
BASE = os.environ.get("SEED_IIIF_BASE", "https://lbiiif.riksarkivet.se")
VOLUME = os.environ.get("SEED_VOLUME", "A0060198")
PAGES = int(os.environ.get("SEED_MAX_PAGES", "3"))

CATALOG = os.environ.get("SEED_CATALOG_URL", "http://127.0.0.1:2333")
NAMESPACE = "bronze"
TABLE = "pages"
DELIM = "$"
#: The dataset's location. Under the namespace, named for the table — so the storage layout and the
#: catalog identifier tell the same story instead of two different ones.
URI = os.environ.get("SEED_URI", f"s3://lance-catalog/{NAMESPACE}/{TABLE}")

OPTS = {
    "aws_access_key_id": os.environ.get("SEED_S3_KEY", "rustfsadmin"),
    "aws_secret_access_key": os.environ.get("SEED_S3_SECRET", "rustfsadmin"),
    "aws_endpoint": os.environ.get("SEED_S3_ENDPOINT", "http://127.0.0.1:9900"),
    "aws_allow_http": "true",
    "aws_region": "us-east-1",
    "s3_express": "false",
}


def register(location: str) -> None:
    """Create the namespace and register the table, so the catalog owns what storage holds.

    ``location`` is RELATIVE to the catalog root, not an s3:// URI. register_table refuses an
    absolute one outright — "Absolute URIs are not allowed for register_table. Location must be a
    relative path within the root directory" — which is the catalog refusing to register storage it
    does not own. Registering an arbitrary bucket would let a caller attach data outside the
    warehouse and have the catalog vouch for it.

    Both calls tolerate "already exists": the script is re-runnable, and a second seed of the same
    volume must not fail on the namespace the first one created.
    """
    with httpx.Client(base_url=CATALOG, timeout=60.0) as http:
        r = http.post(f"/v1/namespace/{NAMESPACE}/create", json={"id": [NAMESPACE], "mode": "EXIST_OK"})
        print(f"  namespace {NAMESPACE!r}: HTTP {r.status_code}")
        if r.status_code >= 400 and "exist" not in r.text.lower():
            sys.exit(f"!! could not create namespace: {r.text[:300]}")

        table_id = f"{NAMESPACE}{DELIM}{TABLE}"
        r = http.post(
            f"/v1/table/{table_id}/register",
            json={"id": [NAMESPACE, TABLE], "location": location, "mode": "OVERWRITE"},
        )
        print(f"  table {table_id!r}: HTTP {r.status_code}")
        if r.status_code >= 400:
            sys.exit(f"!! could not register table: {r.text[:300]}")


def main() -> None:
    src = IIIFVolumeSource(VOLUME, base_url=BASE, query_params="full/max/0/default.jpg", timeout=60.0, max_pages=PAGES)
    res = ingest_to_bronze(src, URI, OPTS, max_objects=10, max_total_bytes=200 << 20)
    print(f"ingested {res.row_count} pages -> {URI}")
    print("registering with the catalog (the governed half):")
    # Relative to the catalog root — see register()'s docstring for why absolute is refused.
    register(f"{NAMESPACE}/{TABLE}")
    print(f"done — {NAMESPACE}{DELIM}{TABLE} is registered, not just written")


if __name__ == "__main__":
    main()
