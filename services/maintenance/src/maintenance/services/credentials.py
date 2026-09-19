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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.table_locations import table_id_from_location


if TYPE_CHECKING:
    from maintenance.core.config import MaintenanceSettings
from maintenance.core.metrics import record_credential_tier
from maintenance.services.catalog_identity import service_headers
from maintenance.services.compaction_executor import MaintenanceDenied, denial_remedy


logger = logging.getLogger(__name__)

#: Short. The door does a describe, a manifest read and an STS exchange; a maintenance tick that
#: blocks longer than this on ONE credential should proceed on the ambient one rather than stall the
#: dataset behind it.
_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class Vended:
    """What the catalog handed back: the scoped options AND where it says that table lives.

    THE LOCATION WAS ALWAYS IN THE RESPONSE AND WAS ALWAYS DISCARDED. `CredentialResponse` carries
    `location` — the catalog's authoritative answer for the table id it was asked about — and this
    module read only `credentials.storage_options`. Keeping it is what makes [[LH-141]]'s crossing
    check cost nothing: the question "does this credential cover the dataset I am holding" is answered
    by a field already on the wire, not by a second round trip the sweep cannot afford (the tick walks
    552 datasets).

    ``None`` where the door named none. A catalog that does not say is not a catalog that disagrees.
    """

    options: dict[str, str]
    location: str | None


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

    A FOURTH condition is checked on the way OUT rather than the way in, because it needs the catalog's
    answer: the vended credential must COVER the dataset being maintained. See
    :func:`_refuse_a_credential_that_does_not_cover` — a credential scoped to another table's location
    is not a weaker credential, it is a credential for a different tenant's data.

    A DECLARED id is never repaired or second-guessed here. If a producer stamps a wrong one the vend
    fails on a table that does exist, which is a visible 403 in the log — whereas silently falling back
    to derivation would hide the producer's bug behind a working sweep.
    """
    if not settings.catalog_url:
        _announce_vending_is_off(declared_table_id or uri, key_id=settings.s3_access_key_id)
        return fallback
    table_id = declared_table_id or table_id_from_location(uri)
    if table_id is None:
        logger.debug("maintenance_vend_skipped_unresolvable_location", extra={"uri": uri})
        return fallback

    vended = _vend(table_id, settings)
    if vended is None:
        # NAME THE KEY, do not rank it. "the root key" is an assertion this function cannot make:
        # `MAINTENANCE_S3_ACCESS_KEY_ID` is `rask-maintenance` on the deployed estate, a scoped
        # identity, so the line reported a posture the estate had already left — and an operator
        # reading it went looking for a hardening that was in place. Emptying the chart value is still
        # a documented way back to the tenant root, which is exactly why the identity has to be read
        # rather than guessed. Matches `vended_credentials.py`'s own wording.
        logger.info(
            "write credential AMBIENT for %s — nothing vended; this rewrite is signed by the process credential %s",
            table_id,
            settings.s3_access_key_id or "<ambient environment>",
        )
        # ON THE SERIES as well as the log. Per-dataset `info` was the only signal, and an estate where
        # EVERY rewrite took this branch read as routine traffic for long enough to be cited as evidence
        # that something else was working.
        record_credential_tier(tier="ambient")
        return fallback
    # AFTER the vend and BEFORE handing it over: the catalog's answer is what says where this table
    # lives, so the crossing is only detectable once there is an answer to read.
    _refuse_a_credential_that_does_not_cover(uri=uri, table_id=table_id, location=vended.location)
    logger.info("write credential SCOPED for %s — this rewrite is signed by a table-scoped credential", table_id)
    record_credential_tier(tier="scoped")
    return vended.options


def _refuse_a_credential_that_does_not_cover(*, uri: str, table_id: str, location: str | None) -> None:
    """Stop the unit when the catalog's location for ``table_id`` is not the dataset being maintained.

    [[LH-141]]. A stale ``lineage.dataset_id`` stamp makes the sweep hold one tenant's dataset while
    addressing another tenant's table. Measured 2026-09-16: ``s3://bind86-wh/medallion/silver`` and
    ``s3://lance-catalog/medallion/gold`` both stamped ``bronze$events``, a table the catalog governs at
    ``s3://lance-catalog/medallion/bronze``. The compaction-plan door answered 200 to every one of
    those — correctly, because it was asked about a table and not about the dataset the caller holds.

    CONTAINMENT, NOT EQUALITY, and getting that backwards would refuse the healthy majority: a branch
    dataset is swept at ``<root>/tree/<branch>`` while the catalog answers with the root, and the live
    sweep holds several. ``base_refs.normalise`` is the estate's one comparator for two spellings of one
    path; re-implementing it here is what its own docstring names as the failure indistinguishable from
    having no guard at all.

    AN UNVERIFIABLE VEND PROCEEDS, loudly. A missing location means the catalog did not say, not that
    it disagrees, and refusing on absence would stop maintaining the estate the day the response shape
    changed — the same asymmetry ``sweep._probe_before_vending`` argues for uncertainty.
    """
    if location is None:
        logger.warning(
            "vended credential for %s carries no location, so it cannot be checked against %s — this rewrite proceeds unverified",
            table_id,
            uri,
        )
        return
    governed, holding = normalise(location), normalise(uri)
    if holding == governed or holding.startswith(f"{governed}/"):
        return
    raise MaintenanceDenied(
        f"the dataset at {uri} declares the table id {table_id!r}, which the catalog governs at {location} — "
        "the two name different locations, so a rewrite here would be signed for one tenant's table and land in "
        "another's. Repair the dataset's `lineage.dataset_id` stamp, or register the id the dataset really is."
    )


#: Whether this process has already said that vending is switched off. The CONDITION is a whole-service
#: one — an empty ``catalog_url`` does not vary between datasets — so it is reported at the volume of the
#: configuration rather than of the sweep. A per-dataset line here would be thousands an hour that never
#: change, which is how § Q17-26's detector drowned the audit trail it shared.
_vending_off_announced = False


def _reset_vending_notice() -> None:
    """Test seam: the notice is once-per-PROCESS, and a test asserting that needs to start from unsaid."""
    global _vending_off_announced
    _vending_off_announced = False


def _announce_vending_is_off(subject: str, *, key_id: str) -> None:
    """Say, once, that every rewrite from here is signed by the root key.

    THIS BRANCH USED TO BE THE SILENT ONE, and it is the branch that covers a whole-service
    misconfiguration — so the module kept its "degrades and says so" promise for a single unreachable
    table and broke it for an entire estate running unhardened. Measured 2026-09-09: the live
    maintenance pod held ``catalog_url = ''`` (``maintenance.vendWriteCredentials`` was the chart
    default), so every compaction signed with the root object-store key and the only evidence was the
    ABSENCE of a stream nobody was watching.
    """
    global _vending_off_announced
    if _vending_off_announced:
        return
    _vending_off_announced = True
    logger.warning(
        "write credential vending is NOT CONFIGURED (no catalog URL) — every rewrite from this process, "
        "starting with %s, is signed by the process credential %s. Set maintenance.vendWriteCredentials to scope it.",
        subject,
        key_id or "<ambient environment>",
    )


def _vend(table_id: str, settings: MaintenanceSettings) -> Vended | None:
    """One vend, or ``None``. Narrow ``except`` on purpose — see `ingest.catalog_service`, where a
    blanket catch reported a `NameError` in the vending method itself as "vending unavailable"."""
    url = f"{settings.catalog_url.rstrip('/')}/management/v1/table/{table_id}/credentials"
    # Both halves, never one: the catalog's identity door requires the app token AND the claimed
    # subject, and sending one is a refusal whose reason is invisible from this side. The token is read
    # from `APP_API_TOKEN`, which daprd injects — `DaprDoorSettings` is the estate's one reader of it,
    # replacing what used to be four bare `os.environ.get` calls.
    headers = service_headers(settings)
    try:
        # `params`, NOT a body. The door declares `tier: Annotated[Tier, Query()] = "read"`, and
        # FastAPI ignores an unknown body on a query parameter — a body-borne tier came back READ with
        # a 200, and the rewrite then failed at the object store as `403 AccessDenied` on a PUT.
        # Measured in-cluster 2026-09-03 on the ingest client, which had exactly this bug.
        response = httpx.post(url, params={"tier": "write"}, headers=headers, timeout=_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        logger.info("credential vending unreachable for %s (%s)", table_id, exc)
        return None
    if response.status_code in (401, 403):
        # A DENIAL, NOT AN OUTAGE — and the difference decides whether a rewrite may proceed. Falling
        # back here hands the caller the deployment's ambient key, which reaches every bucket in the
        # estate, in answer to the catalog saying this identity may not write this one table. Measured
        # live 2026-09-10: eight tables were refused and rewritten under the root credential anyway,
        # visible only as an INFO line nobody reads.
        raise MaintenanceDenied(
            f"the catalog REFUSED a write credential for {table_id} ({response.status_code}) — this rewrite is not "
            f"authorized, and signing it with the ambient key would be a bypass. "
            f"{denial_remedy(table_id=table_id, identity=settings.catalog_service_identity)}"
        )
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
    if not isinstance(options, dict):
        return None
    location = payload.get("location")
    return Vended(
        options={str(key): str(value) for key, value in options.items()},
        location=str(location) if location else None,
    )
