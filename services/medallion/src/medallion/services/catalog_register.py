"""Registration of a stage's written dataset in the catalog — the governance half of every write.

The cascade's writes were governed by PATH CONVENTION only: no `register_table` call existed
anywhere in the medallion, so a tier's output was a dataset the catalog never heard of —
unprotectable, untrashable, and invisible to the FGA doors #90 gated. Registration is what turns
the written bytes into a `table:` object: the catalog's register door seeds ownership tuples, and
every governed read path (the viewer's pages, credentials vending, protection) keys off that
object.

WORKLOAD-NEUTRAL BY CONSTRUCTION, and that is the point of the file's name. This shipped named for
one workload and called from that workload's stage alone, so the lane built first was governed and
every other lane wrote ungoverned bytes. Nothing in the logic was ever workload-specific: it takes
an id and a URI. Governance belongs to the CASCADE, or every new workload starts ungoverned by
default — the exact opposite of an agnostic platform.

THE STAGE RUNNER ASKS THE CATALOG, IT DOES NOT TELL IT (rule I2, write side). Composing a location —
`{root}/medallion/{tier}` — and registering it after the write is the defect this seam exists to
close: the catalog's namespace binding may resolve somewhere else entirely, so the publish that
follows opens the catalog's answer and finds nothing there. `ensure_stage_output` therefore creates
through the catalog's OWN door (`POST /v1/table/{id}/create` when `describe` 404s) and takes the
location from that response, and `transform.py` calls it BEFORE the write. Registration is still the
goal; asking first is how the goal is reached.

A SECOND, READ-ONLY DOOR sits beside it: `describe_table_location`, which asks where a table that
ALREADY EXISTS lives and creates nothing. It is what lets a consumer — the cascade head, which writes
no table of its own — name the upstream a stage runner should open instead of leaving it to compose a path.
The bronze tier is written by a client through the catalog's own namespace doors (ADR 0154), so the
catalog vends its location and only the catalog can say where it is.

Registration strictly precedes the first row, so there is no window
in which rows exist that the catalog has no record of.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import httpx
import pyarrow as pa
from pydantic import BaseModel, Field

from service_kit.governed.machine_identity import IdentityTokenUnavailableError, identity_bearer
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


log = logging.getLogger(__name__)


class RegisterError(RuntimeError):
    """The catalog refused or could not be reached — the stage must NOT report success.

    An unregistered gold table is #88's defect intact, so this propagates and the stage runner RETRYs.
    That re-runs the (expensive) transcribe too — stated cost: the overwrite is idempotent and a
    catalog outage is rarer than a Serve one; splitting the stage into resumable halves is P7b's
    re-cut, not a quiet retry layer here.
    """


def credential(identity_token_file: str) -> dict[str, str]:
    """The credential a medallion service presents to the catalog: its own projected token, read now.

    The service-account token the kubelet projects with audience `rask-catalog` is the whole credential
    ([[LH-220]], D1): the catalog verifies it offline and maps its account to the subject the grants name,
    so no header names the caller. Read on every call, because the kubelet rotates it at 515 s.

    A token that cannot be read is a `RegisterError`, so the call is not sent rather than sent anonymously,
    and the stage RETRYs exactly as it does for an unreachable catalog.
    """
    try:
        return identity_bearer(identity_token_file)
    except IdentityTokenUnavailableError as exc:
        raise RegisterError(f"this service's catalog identity token is unavailable: {exc}") from exc


class PublishOutcome(BaseModel):
    """What the catalog decided about a version the stage runner just wrote.

    A REFUSAL is not an error: the run committed its output and did its job, and it is the DATA that
    was refused. `failed_assertions` is what the stage runner needs to decide whether a person should be
    asked — structural findings are unanswerable, the rest are reviewable.
    """

    published: bool
    from_version: int | None = None
    to_version: int | None = None
    failed_assertions: list[str] = Field(default_factory=list)
    accepted: list[str] = Field(default_factory=list)


def publish_stage_output(
    *,
    catalog_url: str,
    table_id: str,
    version: int,
    key_column: str,
    required_columns: Sequence[str] = (),
    accept_assertions: Sequence[str] = (),
    identity_token_file: str,
    timeout_seconds: float = 30.0,
    gate_only: bool = False,
    cascade_id: str = "",
    originator: str = "",
    client: httpx.Client | None = None,
) -> PublishOutcome:
    """Ask the catalog to gate `version` and, if it passes, advance the `published` tag.

    With ``gate_only`` it asks for the VERDICT and nothing else: the same assertions on the same
    version, `published` false and the tag untouched. That is what lets a caller decide a promotion
    review BEFORE promoting — under a publish-driven cascade the tag move IS the promotion, so a
    review that runs after it has nothing left to withhold, and one that runs before it cannot name
    the assertions unless it can ask first. Same door and same rung either way: only a caller who
    could publish has any business asking whether this door would accept a version.

    THE OTHER HALF OF REGISTERING. A commit makes the output readable; this is what makes it READY,
    and it is the catalog's operation so that every writer — this stage runner, a Ray job, a backfill —
    publishes identically and meets the same rung and the same assertions.

    It replaces the stage runner's own gate rather than joining it. Both ran the same checks; the local one
    withheld only the next TRIGGER, so a refused batch was already committed into the tier and visible
    to anyone reading `latest`. Only the tag is a boundary.

    Raises on an unreachable or refusing catalog — an outage or a 403 is not a data verdict, and
    reading one as a quality refusal would report a governance failure as a bad batch.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot publish its output table")
    headers = credential(identity_token_file)
    body = {
        "version": version,
        "key_column": key_column,
        "required_columns": list(required_columns),
        "accept_assertions": list(accept_assertions),
        "gate_only": gate_only,
        # Echoed by the catalog onto `table_published`, which is the ONE hop where a batch identity
        # would otherwise be lost — the publication head mints the next token from the event id.
        "cascade_id": cascade_id,
        # THE HUMAN THE BATCH IS FOR, across the same lost hop and for the same reason. A stage runner
        # authenticates to this door AS ITSELF (`credential` above), so the control event's actor is the
        # subject its service account maps to — an inbox actor named after a stage runner, which is worse than silence because
        # it looks delivered. The person is only in this body, and the catalog decides what to do with
        # the claim (`publication_originator`): it authorizes nothing here and the notifications plane
        # re-derives every recipient's visibility at delivery.
        "originator": originator,
    }
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            response = client.post(f"/management/v1/table/{table_id}/publish", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable publishing {table_id!r}: {exc}") from exc
    if response.status_code >= 400:
        raise RegisterError(f"catalog refused the publish of {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    payload = response.json()
    return PublishOutcome(
        published=bool(payload.get("published")),
        from_version=payload.get("from_version"),
        to_version=payload.get("to_version"),
        failed_assertions=[a["assertion"] for a in payload.get("assertions") or [] if not a.get("success")],
        accepted=list(payload.get("accepted") or []),
    )


def authorize_stage_write(
    *,
    catalog_url: str,
    table_id: str,
    identity_token_file: str,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> str:
    """Prove this stage runner may WRITE `table_id`, at the catalog's own door. Returns the vending mode.

    CALLED FOR ITS SIDE EFFECT, and the answer is deliberately discarded by the caller. The stage job
    opens its destination with the RustFS ROOT credential from the Ray pod's environment and performs
    no authorization at all — so of every door in this estate, the one that actually moves a tenant's
    bytes was the one answering to nothing. This does not close that: with `LANCE_VENDING_MODE=mode_b`
    the vendor returns nothing (`core/vending.py`), the response is `server_mediated`, and the bytes
    still move under the pod credential. THE BYTE PATH IS UNCHANGED ON PURPOSE — a change that also
    re-routed the IO would be a much larger one, and this estate's cascade is load-bearing.

    What it does buy is the two things that were absent entirely: the WRITE rung is checked
    (`can_write_data` on the table, which the catalog evaluates only on the write tier — the read
    tier carries the router guard's rung alone), and the decision lands in the audit stream keyed to
    the table and the tier, allow or deny alike.

    THE WRITE TIER SPECIFICALLY. Asking for `read` would pass a rung the cascade does not need and
    make a reader's grant indistinguishable from a writer's on the one path that writes.

    Raises on a refusal. A 403 means this stage runner may not write the table it is about to write, and a
    stage that proceeds anyway makes the check decorative — which is the failure mode of every
    authorization added for tidiness rather than for a decision.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot authorize its write")
    headers = credential(identity_token_file)
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            response = client.post(f"/management/v1/table/{table_id}/credentials", params={"tier": "write"}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable authorizing the write of {table_id!r}: {exc}") from exc
    if response.status_code >= 400:
        raise RegisterError(f"catalog refused this stage's write of {table_id!r}: HTTP {response.status_code} — {response.text[:300]}")
    return str(response.json().get("mode") or "")


@contextmanager
def _catalog_client(catalog_url: str, timeout_seconds: float, client: httpx.Client | None) -> Iterator[httpx.Client]:
    """The shared client when the caller has one, otherwise a per-call client it owns.

    `fastapi` -> `production-patterns.md` § Lifespan wants one client built once and injected; the
    stage runner's lifespan now builds it. The fallback is not laziness — every OTHER caller of these helpers
    (the tests, `scripts/`, any direct use) has no app and no lifespan, and making the client mandatory
    would break them to satisfy a rule about the hot path. A caller that passes one must keep owning
    it: closing it here would shut the app's client after the first stage.
    """
    if client is not None:
        yield client
        return
    with httpx.Client(base_url=catalog_url.rstrip("/"), timeout=timeout_seconds) as owned:
        yield owned


def ensure_stage_output(
    *,
    catalog_url: str,
    table_id: str,
    schema: pa.Schema,
    external_blob_base: str | None = None,
    delimiter: str = CATALOG_DELIMITER,
    identity_token_file: str,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> str:
    """Ask the catalog where this stage's output lives, creating the table if it does not exist yet.

    ``external_blob_base`` is the upstream's registered external blob base, asked for on the create
    through the door's ``external_blob_base`` parameter ([[LH-209]]). The stage forwards the upstream's
    pointers, which resolve only under a base the output registered, and the catalog registers none it
    was not asked for, so without it every carried pointer is refused at the stage's first write.

    THE STAGE RUNNER ASKS INSTEAD OF TELLING, which is rule I2 applied to the write side. `transform.py` says
    the quiet part: I2 was "read from the consuming end. Only the READ side: the stage runner still owns where
    it WRITES." That half is the defect. The stage runner composed `{root}/medallion/{tier}` — a layout the
    catalog has never vended — wrote there, and then registered that path. The catalog's binding said
    somewhere else, so the publish that followed opened the catalog's answer and found nothing.

    The shape is the ingest plane's, which has been doing this correctly all along
    (`ingest.catalog_service.CatalogServiceClient.ensure`): describe, create when absent, and take the
    location from the CREATE's own response rather than re-asking a read door — `describe` answers 403
    for an absent table, so believing it is what made a new table impossible to create at all.

    THAT SAME 403 IS ALSO HOW AN UNGOVERNED TABLE PRESENTS, which is why the create below asks for
    `exist_ok`. A table carrying no FGA tuples denies every relation to everyone, so `describe` refuses
    it exactly as it refuses an absent one, and a default-mode create then collides 409 with the table
    that is really there. ExistOk makes the two cases converge on the same answer instead.

    ``schema`` only has to be A schema, not the output's: the empty table exists so the catalog mints
    and governs a URI, and the stage runner's `overwrite` replaces the schema wholesale afterwards. A stage
    does not know its output schema until it has computed, and does not need to.

    SENT WITHOUT ITS SCHEMA METADATA. Callers pass the UPSTREAM's schema, which carries the upstream's
    `lineage.*` stamp, and the catalog refuses a create payload naming a reserved key ([[LH-208]]). The
    stamp names the upstream in any case, so it would be a false claim about this table.

    Never falls back to a composed path. A catalog that vends no location is an error — guessing one
    is precisely the defect this replaces.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this stage cannot resolve where to write")
    headers = credential(identity_token_file)
    segments = table_id.split(delimiter)
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            described = client.post(f"/v1/table/{table_id}/describe", json={}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable resolving {table_id!r}: {exc}") from exc
        if described.status_code == 200:
            return _vended(described, table_id)

        try:
            # `mode=exist_ok`, NOT the default `Create`, and it is load-bearing rather than defensive.
            # Reaching here means `describe` refused, and that has TWO causes which the door cannot
            # tell apart: the table is absent (the ordinary case this seam exists for), or it exists
            # and is UNGOVERNED — zero FGA tuples, so every relation on it resolves False and the read
            # door denies exactly as it would for a missing table. Under the default mode the second
            # case answers 409 and the stage dies, which is the state five of the estate's own tiers
            # were measured in on 2026-09-15.
            #
            # ExistOk converges instead of colliding, and it CANNOT lose data: `table_create` computes
            # pre-existence from a NATIVE check rather than the gated describe, keeps the existing
            # table untouched, and writes the structural `parent` edge that was missing — the edge
            # alone, never `owner`, so a no-op create can still not seize somebody's table.
            created = client.post(
                f"/v1/table/{table_id}/create?mode=exist_ok",
                content=encode_arrow_stream(schema.remove_metadata().empty_table()),
                params={"external_blob_base": external_blob_base} if external_blob_base else None,
                headers={**headers, "Content-Type": ARROW_STREAM_MEDIA_TYPE, "x-lance-table-id": delimiter.join(segments)},
            )
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable creating {table_id!r}: {exc}") from exc
        if created.status_code >= 400:
            raise RegisterError(f"catalog refused to create {table_id!r}: HTTP {created.status_code} — {created.text[:300]}")
        return _vended(created, table_id)


def _stated_location(response: httpx.Response) -> str:
    """The location a catalog response states, or ``""`` — ``table_uri`` first, then ``location``.

    ONE reading of that precedence, because the create/describe doors do not agree on the key and two
    spellings of "where does this table live" is exactly how a caller ends up reading the field the
    other door does not set. What a caller does with an empty answer is its own decision.
    """
    payload = response.json() or {}
    return str(payload.get("table_uri") or payload.get("location") or "")


def _vended(response: httpx.Response, table_id: str) -> str:
    """The location the catalog stated, or an error naming that it stated none."""
    location = _stated_location(response)
    if not location:
        raise RegisterError(f"the catalog returned no location for {table_id!r} — refusing to compose one, which is the defect this call replaces")
    return location


def describe_table_location(
    *,
    catalog_url: str,
    table_id: str,
    identity_token_file: str,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> str | None:
    """Where the catalog says ``table_id`` lives, or ``None`` when it governs no such table.

    THE READ SIDE OF I2, for a caller that WRITES NOTHING. `ensure_stage_output` is the write-side
    question ("where should I put my output, create it if absent"); this is the read-side one ("where
    is the table that already exists"), and it must never create: a cascade head asking where a bronze
    table lives has no business minting one, and a create here would attach a `table:` object to bytes
    nobody has written.

    ``None`` IS AN ANSWER, NOT A FAILURE. The catalog answers 4xx for a table it does not govern (and
    `describe` answers 403 rather than 404 for an absent one, which is why the status is not
    interrogated further) — an external OpenLineage producer writing an unregistered dataset is a real
    and supported case, and the caller's composed-path fallback is the right behaviour for it.

    Raises :class:`RegisterError` only when the catalog could not be ASKED — an unreachable service or
    a 5xx. That is a different thing from "no such table", and collapsing the two would let an outage
    read as a governance answer.
    """
    if not catalog_url:
        raise RegisterError("MEDALLION_CATALOG_URL is not set — this caller cannot ask where a table lives")
    headers = credential(identity_token_file)
    with _catalog_client(catalog_url, timeout_seconds, client) as client:
        try:
            described = client.post(f"/v1/table/{table_id}/describe", json={}, headers=headers)
        except httpx.HTTPError as exc:
            raise RegisterError(f"catalog unreachable resolving where {table_id!r} lives: {exc}") from exc
        if described.status_code >= 500:
            raise RegisterError(f"catalog could not say where {table_id!r} lives: HTTP {described.status_code} — {described.text[:300]}")
        if described.status_code >= 400:
            return None
        return _stated_location(described) or None
