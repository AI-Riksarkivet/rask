"""Seed ``bronze$pages`` with REAL Riksarkivet page images — REGISTERED IN THE CATALOG.

Why this exists: the chart's producer ships without MEDALLION_COMPUTE_ENABLED /
the page-lane bronze URI, so the retired medallion ingest head answered 409 ("head is not
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

import logging
import os
import sys
from collections.abc import Iterator

import httpx
from medallion.services.ingest import ingest_to_bronze

from service_kit.lakehouse.sources import SourceObject


# ── IIIF helpers, INLINE on purpose ──────────────────────────────────────────────
# This script straddles both planes: it needs `medallion.services.ingest` (root workspace) and a
# IIIF fetch, and a sealed runner cannot import a platform service. The full read-through cache
# lives in `runners/htr/src/htr/iiif.py`, owned by the workload that uses it — re-exporting IIIF
# from `packages/storage` is what put one workload's protocol into every deployment. A second
# consumer is the signal to build a generic HTTP source behind `build_source`, not to promote
# these back into a package.
log = logging.getLogger(__name__)

DEFAULT_IIIF_BASE = "https://iiifintern-ai.ra.se"
DEFAULT_QUERY_PARAMS = "full/max/0/default.jpg"


def get_image_ids(
    batch_id: str,
    *,
    base_url: str = DEFAULT_IIIF_BASE,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> list[str]:
    """Fetch a IIIF manifest and return sorted image IDs.

    Riksarkivet manifest items have ids of the form
    `https://iiifintern-ai.ra.se/arkis!A0060198_00001/canvas`; we keep the
    first 14 chars after the `!` (e.g. `A0060198_00001`).
    """
    manifest_url = f"{base_url}/arkis!{batch_id}/manifest"
    log.info("Fetching manifest: %s", manifest_url)

    if client is None:
        with httpx.Client(timeout=timeout) as c:
            resp = c.get(manifest_url)
            resp.raise_for_status()
            data = resp.json()
    else:
        resp = client.get(manifest_url)
        resp.raise_for_status()
        data = resp.json()

    image_ids: list[str] = []
    for item in data.get("items", []):
        raw_id = item.get("id", "")
        if "!" in raw_id:
            image_ids.append(raw_id.split("!")[1][:14].upper())

    log.info("Found %d images in batch %s", len(image_ids), batch_id)
    return sorted(image_ids)


def build_image_url(
    image_id: str,
    *,
    base_url: str = DEFAULT_IIIF_BASE,
    query_params: str = DEFAULT_QUERY_PARAMS,
) -> str:
    """Build a IIIF Image API URL for a given image ID."""
    return f"{base_url}/arkis!{image_id}/{query_params}"


def file_extension(query_params: str = DEFAULT_QUERY_PARAMS) -> str:
    """Extract the file extension implied by the IIIF query params."""
    return "." + query_params.rsplit(".", 1)[-1]


def fetch_image(url: str, *, client: httpx.Client, attempts: int = 3, base_delay: float = 1.0) -> bytes:
    """GET one IIIF image with retry on transient httpx errors and 5xx — the shared reading primitive.

    The IIIF server hands out RST under load (Errno 104 at ~64 concurrent reads); three tries with
    exponential backoff convert a transient hiccup into a slowdown instead of a pipeline kill. Real 4xx
    (except 429) raise immediately — those won't get better. Used by both :class:`IIIFCachedSource` (the
    legacy cache role) and the medallion IIIF→raw producer (the P7a harvest library role).
    """
    import time as _time

    last: Exception | None = None
    for i in range(attempts):
        try:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.content
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                code = exc.response.status_code
                if code != 429 and 400 <= code < 500:
                    raise
            last = exc
            if i < attempts - 1:
                _time.sleep(base_delay * (2**i))
    assert last is not None
    raise last


#: The PUBLIC Riksarkivet IIIF endpoint. The chart defaults to https://iiifintern-ai.ra.se, which
#: resolves only on RA's network, so a developer anywhere else cannot harvest at all.
BASE = os.environ.get("SEED_IIIF_BASE", "https://lbiiif.riksarkivet.se")
VOLUME = os.environ.get("SEED_VOLUME", "A0060198")
PAGES = int(os.environ.get("SEED_MAX_PAGES", "3"))

CATALOG = os.environ.get("SEED_CATALOG_URL", "http://127.0.0.1:2333")
#: The catalog accepts ONLY IdP bearers when auth is on, and register() is a governed write — so a
#: seed against an auth-enabled estate must carry one or every registration 401s. Empty = auth off.
CATALOG_TOKEN = os.environ.get("SEED_CATALOG_TOKEN", "")
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


def _tolerate_only_already_exists(response: httpx.Response, what: str) -> None:
    """Converge on 409, die on everything else.

    The tolerance used to be `"exist" not in r.text.lower()`, and the catalog renders NOT-FOUND as
    prose containing that substring — "Namespace does not exist", "Table does not exist". So a
    register into a namespace whose create had silently failed was swallowed and then reported as
    "already registered — converged", which is exactly backwards: it is the ungoverned-data bug this
    script exists to prevent, reintroduced by its own idempotency. The test belongs on the status
    code (ALREADY_EXISTS is 409 across the board — see service_kit.lakehouse.ns_errors), never prose.
    """
    if response.status_code < 400 or response.status_code == 409:
        return
    sys.exit(f"!! could not {what}: HTTP {response.status_code} {response.text[:300]}")


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
    headers = {"Authorization": f"Bearer {CATALOG_TOKEN}"} if CATALOG_TOKEN else {}
    with httpx.Client(base_url=CATALOG, timeout=60.0, headers=headers) as http:
        r = http.post(f"/v1/namespace/{NAMESPACE}/create", json={"id": [NAMESPACE], "mode": "EXIST_OK"})
        print(f"  namespace {NAMESPACE!r}: HTTP {r.status_code}")
        _tolerate_only_already_exists(r, "create namespace")

        table_id = f"{NAMESPACE}{DELIM}{TABLE}"
        r = http.post(
            f"/v1/table/{table_id}/register",
            json={"id": [NAMESPACE, TABLE], "location": location, "mode": "OVERWRITE"},
        )
        print(f"  table {table_id!r}: HTTP {r.status_code}")
        # A re-seed must CONVERGE, not fail: the table this run would register is the table a
        # previous run already registered, pointing at the same location. `mode: OVERWRITE` does
        # not cover an existing REGISTRATION, so an already-exists answer is success — the same
        # tolerance the namespace create above applies, and what makes `make seed-dev` safe to run
        # twice.
        _tolerate_only_already_exists(r, "register table")
        if r.status_code == 409:
            print(f"  table {table_id!r}: already registered — converged")


class IIIFVolumeSource:
    """One IIIF volume as a :class:`~service_kit.lakehouse.sources.SourceAdapter`.

    **Why this lives in the script rather than in a service.** It used to be
    ``medallion.services.iiif_produce.IIIFVolumeSource``, deleted by ``09823f56`` ("the medallion
    sheds acquisition") — and this file kept importing it, so it has raised ``ModuleNotFoundError``
    before doing anything ever since, taking step 3 of ``make seed-dev`` with it. It was the last
    ``ty`` diagnostic in the repo.

    Repointed rather than deleted, and NOT at the ingest plane, because both of the obvious moves are
    wrong: deleting it breaks ``scripts/seed_dev_estate.sh`` (which runs this file) and leaves the
    estate with no bronze-page seed at all, while ``/api/ingest`` has no IIIF door to point at —
    ``ingest.adapters`` registers local-dir and s3-prefix only, the ``iiif`` source kind having been
    removed by explicit ruling. That ruling was about IIIF as a WATCHED SINK in a service; a one-shot
    developer seed is the explicit snapshot importer it left room for. So the acquisition lives HERE,
    where the one caller is, built on the surviving ``storage`` IIIF helpers — the same
    manifest → URL → fetch sequence ``scripts/download_iiif.py`` already performs.

    The adapter surface is deliberately the whole of ``SourceAdapter``: one ``iter_objects``. Nothing
    else is needed, and ``KeyedSourceAdapter`` (enumerate-without-fetch) buys nothing for a seed of
    three pages that is going to fetch all of them anyway.
    """

    def __init__(self, volume: str, *, base_url: str, query_params: str, timeout: float, max_pages: int) -> None:
        self.volume, self.base_url, self.query_params = volume, base_url, query_params
        self.timeout, self.max_pages = timeout, max_pages

    def iter_objects(self) -> Iterator[SourceObject]:
        # ONE client for the manifest and every page: connection reuse, and a single place the
        # timeout is set. Closed on the way out even if the caller stops consuming early.
        with httpx.Client(timeout=self.timeout) as client:
            image_ids = get_image_ids(self.volume, base_url=self.base_url, timeout=self.timeout, client=client)
            for image_id in image_ids[: self.max_pages]:
                url = build_image_url(image_id, base_url=self.base_url, query_params=self.query_params)
                # `iiif://<volume>/<image>` and not the fetch URL: the provenance recorded in bronze
                # must survive `SEED_IIIF_BASE` pointing at RA's internal endpoint on one machine and
                # the public one on another. The URI names the OBJECT; the base names the route to it.
                yield SourceObject(uri=f"iiif://{self.volume}/{image_id}", data=fetch_image(url, client=client))


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
