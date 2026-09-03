"""Per-table WRITE credentials for the maintenance plane, from the catalog's vending door.

WHY THIS SERVICE, WHICH IS NOT OBVIOUS: maintenance holds the root object-store key and rewrites
fragments with it. Compaction is a write — it lands new data files and commits a new manifest — so a
service performing it with a long-lived key that reaches every bucket is the exact posture the vending
door exists to end. What it holds at any moment becomes a credential scoped to ONE table prefix,
expiring in 900s, and issued against an audited decision (`vend_credentials` records who asked, for
what table, at what tier).

**NO CACHE, deliberately, and the access pattern is the reason.** The ingest worker vends once and
writes millions of units behind it, so it needs a cache that refreshes before expiry
(`ingest/credentials.py`). Maintenance vends once per WORK ITEM — one dataset, one compaction — and
the broker's `ackWait` is 720s against a 900s credential, so a unit that outlives its credential has
already outlived its delivery. A cache here would hold credentials for datasets this replica may never
see again.

**WHAT STAYS ON THE ROOT KEY, stated rather than glossed:** discovery and the protection pre-pass.
`sweep._protected_roots` must open every manifest in every bucket before ANY dataset is compacted,
because a shallow clone in bucket B is the only thing that knows bucket A's dataset must not be
rewritten — no per-table credential can express a whole-estate read, and narrowing it would silently
turn the guard off. Those are READS. The clause this serves is that no service holds a root key on a
WRITE path, and the write path is here.

Every failure degrades to the ambient credential and says so. Vending is a hardening, and a hardening
that can fail a maintenance run turns an optional improvement into a new way to stop reclaiming disk.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from service_kit.governed.dapr_auth import DaprDoorSettings
from service_kit.lakehouse.table_locations import table_id_from_location


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings


logger = logging.getLogger(__name__)

#: Short. The door does a describe, a manifest read and an STS exchange; a maintenance tick that
#: blocks longer than this on ONE credential should proceed on the ambient one rather than stall the
#: dataset behind it.
_TIMEOUT_SECONDS = 10.0


def write_options_for(uri: str, settings: MaintenanceSettings, *, fallback: dict[str, str], declared_table_id: str | None = None) -> dict[str, str]:
    """The storage options this dataset's REWRITE should be signed with.

    Returns the vended, table-scoped options when every precondition holds, and ``fallback`` — the
    deployment's ambient credential, which is what this always used — otherwise. The three
    preconditions are checked in the order that makes a misconfiguration readable:

    1. a catalog URL is configured. Unset means this deployment has no vending door;
    2. a table identifier is available. ``declared_table_id`` — the id the WORK ITEM carries — is
       preferred over deriving one from the path, and the difference is most of the estate rather than
       an edge case. Measured on the live warehouse: of eleven top-level roots in
       ``s3://lance-catalog/``, :func:`table_id_from_location` answers for six, and the five it cannot
       read include ``medallion/``, the cascade. Those five are NOT unknown to the catalog —
       ``bronze$events`` at ``medallion/bronze`` and ``bronze$pages`` at ``bronze/pages`` both answer a
       write-tier vend with 200 — so deriving alone would leave the highest-churn writer in the estate
       signing with the root key while reporting no fault at all. Derivation stays as the fallback for
       a unit produced before the field existed, or by a planner that genuinely does not know;
    3. the door answers ``direct``. ``server_mediated`` is a supported posture (`mode_b`), not a fault.

    A DECLARED id is never repaired or second-guessed here. If a producer stamps a wrong one the vend
    fails on a table that does exist, which is a visible 403 in the log — whereas silently falling back
    to derivation would hide the producer's bug behind a working sweep.
    """
    if not settings.catalog_url:
        return fallback
    table_id = declared_table_id or table_id_from_location(uri)
    if table_id is None:
        logger.debug("maintenance_vend_skipped_unresolvable_location", extra={"uri": uri})
        return fallback

    vended = _vend(table_id, settings)
    if vended is None:
        logger.info("write credential AMBIENT for %s — nothing vended; this rewrite is signed by the root key", table_id)
        return fallback
    logger.info("write credential SCOPED for %s — this rewrite is signed by a table-scoped credential", table_id)
    return vended


def _vend(table_id: str, settings: MaintenanceSettings) -> dict[str, str] | None:
    """One vend, or ``None``. Narrow ``except`` on purpose — see `ingest.catalog_service`, where a
    blanket catch reported a `NameError` in the vending method itself as "vending unavailable"."""
    url = f"{settings.catalog_url.rstrip('/')}/v1/table/{table_id}/credentials"
    # Both halves, never one: the catalog's identity door requires the app token AND the claimed
    # subject, and sending one is a refusal whose reason is invisible from this side. The token is read
    # from `APP_API_TOKEN`, which daprd injects — `DaprDoorSettings` is the estate's one reader of it,
    # replacing what used to be four bare `os.environ.get` calls.
    token = DaprDoorSettings().app_api_token
    headers = {"x-lance-service-identity": settings.catalog_service_identity}
    if token:
        headers["dapr-api-token"] = token
    try:
        # `params`, NOT a body. The door declares `tier: Annotated[Tier, Query()] = "read"`, and
        # FastAPI ignores an unknown body on a query parameter — a body-borne tier came back READ with
        # a 200, and the rewrite then failed at the object store as `403 AccessDenied` on a PUT.
        # Measured in-cluster 2026-09-03 on the ingest client, which had exactly this bug.
        response = httpx.post(url, params={"tier": "write"}, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.info("credential vending unreachable for %s (%s)", table_id, exc)
        return None
    if response.status_code >= 400:
        logger.info("credential vending unavailable for %s (%s)", table_id, response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.info("credential vending returned an unparseable body for %s", table_id)
        return None
    if payload.get("mode") != "direct":
        return None
    options = (payload.get("credentials") or {}).get("storage_options")
    return {str(key): str(value) for key, value in options.items()} if isinstance(options, dict) else None
