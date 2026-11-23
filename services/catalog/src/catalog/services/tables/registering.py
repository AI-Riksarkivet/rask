"""How a dataset that already exists enters the catalog, and how a table's location is taken back at an undrop: the
registration's admission against its claim and its base record, its convergence when made again, and its undoing.

The door authorizes the caller, seeds and takes back grants and files what it announces; nothing here reads a request
or a grant.
"""

from __future__ import annotations

import logging
from functools import partial

from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    DeregisterTableRequest,
    DescribeTableRequest,
    InternalError,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    RegisterTableRequest,
    UnsupportedOperationError,
)
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.core import native, provenance_guard
from catalog.core.config import Settings
from catalog.core.formats import refuse_properties_kept_nowhere, reject_unsupported_properties
from catalog.core.modes import RegisterMode
from catalog.core.namespace import RegisteredDataset, registered_dataset_facts
from catalog.services import clone_endings, commit_reconcile, table_bases, table_claims
from lakehouse.process.supervision import is_fault
from lakehouse.records import base_registry, location_claims, object_records, trash
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)


def registration_points_at(registered: str, claimed: str) -> bool:
    """Whether an existing ABSOLUTE registration is the location a register body CLAIMS.

    The claim is relative (`8f3a_ns$t` or `t.lance`), the registration absolute
    (`s3://bucket/8f3a_ns$t`), so this asks whether the claim is the registration's TAIL on a path
    BOUNDARY. A bare `endswith` would accept `ns$t` against `s3://b/other_ns$t`, which is a different
    table — and the whole point of the check is that a 409 at a different location is a genuine id
    conflict this must not converge.
    """
    left, right = registered.rstrip("/"), claimed.rstrip("/")
    return left == right or left.endswith("/" + right)


def absolute_table_location(ns: LanceNamespace, segments: list[str], registered: str | None) -> str | None:
    """The table's ABSOLUTE location, falling back to what the register returned.

    `register_table` ECHOES the caller's own path: measured 2026-09-11 on a `dir` namespace, registering
    `location="t.lance"` answers `response.location == 't.lance'` while `describe_table` on the same
    table answers `/<root>/t.lance`. Emitting the echo puts a relative URI on the CREATED edge and on the
    control event's `location` extra.

    WHAT THAT COSTS IS A LOCATION NO READER CAN OPEN: the `source_uri` rides the event into the feed
    and on to a forwarded graph backend as the dataset's dataSource, and a relative path there names
    nothing outside the caller's own working directory. Nothing re-runs to correct it, because the feed
    keeps the event it was given.

    THE BACKEND RESOLVES IT AGAINST THE RIGHT ROOT, which is why this asks rather than joining against a
    configured one: a table belongs to its warehouse's root, and a caller-side join would
    be wrong for exactly the tenant tables that matter.

    NEVER FAILS THE REGISTER. The registration is already committed, so an unreachable describe or an
    empty answer leaves the registered value — no worse than before, where raising would turn a
    successful register into a 500 the caller cannot retry into a better state. Neither caller judges
    with it: the 409 branch compares the resolved location against the body's, and a fresh registration
    keys its location claim by it ([[LH-204]]).
    """
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=segments))
    except BaseException as exc:  # noqa: BLE001 — any failure degrades to the registered value, never to a 500, a native panic too
        if not is_fault(exc):
            raise
        log.warning("register_location_unresolved", extra={"table": segments, "error": str(exc)})
        return registered
    return getattr(described, "location", None) or registered


class Registration(BaseModel):
    """What every step of one registration shares: the table it registers, the uuid it minted, and the store it is in."""

    # `arbitrary_types_allowed` for `ns`: a LanceNamespace is no pydantic type.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str
    segments: list[str]
    canonical: str
    table_uuid: str
    #: The location as the caller named it, relative to the namespace root.
    relative: str
    # A namespace connection is opaque: what the door was handed is what it uses, a stand-in in a test included.
    ns: SkipValidation[LanceNamespace]
    settings: Settings
    so: StorageOptions
    claims: location_claims.ClaimStore
    context: table_bases.RegistrationContext


class Admission(BaseModel):
    """What admitting a registered dataset decided: where its claim is keyed, where it resolves, and what it took."""

    model_config = ConfigDict(frozen=True)

    claimed_location: str
    location: str
    admitted: table_bases.Admitted


def refuse_an_unservable_registration(body: RegisterTableRequest) -> None:
    """Refuse a registration the register door does not serve, from the request alone."""
    # `mode` WAS ACCEPTED AND NEVER READ. The generated model states two: "Create (default): the
    # operation fails with 409. Overwrite: the existing table registration is replaced with the new
    # registration." This door passes `body` straight to the backend, and the backend ignores the
    # field — measured 2026-09-13 on the namespace twin, where every mode raised the same conflict —
    # so a caller asking to REPLACE a registration got the 409 that means "it already exists".
    #
    # Replacing a registration is not implemented here, and refusing says which of the two it is. It
    # is also not a cosmetic difference: `register_table` ATTACHES bytes the catalog does not own, so
    # silently treating "replace" as "conflict" leaves the caller believing their new location was
    # rejected as a duplicate rather than never attempted.
    #
    # A value outside those two is refused by the parse itself, as InvalidInput naming it — `ExistOk`
    # included, which the spec gives `create` and not the register door (`RegisterMode`).
    #
    # Declined as the spec's Unsupported (406, code 0), which RegisterTable lists: the mode is well formed, and this
    # catalog does not serve it.
    if RegisterMode.parse(body.mode) is RegisterMode.OVERWRITE:
        raise UnsupportedOperationError(
            "mode 'Overwrite' is not supported on this door: replacing a registration would detach bytes this "
            "catalog does not own from the table that currently points at them. Deregister the existing table "
            "first, then register the new location."
        )
    # LANCE-ONLY (2026-08-15 ruling), the platform's keys and every other property, as at `declare_table`.
    reject_unsupported_properties(body.properties)
    provenance_guard.refuse_reserved_keys(body.properties or {}, door="register properties")
    refuse_properties_kept_nowhere(body.properties, door="register")


async def converge_a_registration_made_again(registration: Registration) -> None:
    """Write the base record of a table registered again where it stands, when its dataset is judged clean; else nothing."""
    # THE TABLE'S BASE RECORD STILL CONVERGES, AND ONLY WHEN THE LOCATION MATCHES. A 409 at a different location is a
    # genuine id conflict, a different table living there. Same location means this IS that table, so its record is the
    # one a fresh registration would have written. Its caller answers the 409 either way.
    registered = await run_in_threadpool(absolute_table_location, registration.ns, registration.segments, None)
    # THE CLAIM IS CHECKED AS THE TAIL OF THE REGISTRATION, not by equality. The body carries a RELATIVE location — the
    # dir backend refuses an absolute one on the register door — while the registered one is absolute. Resolving both through
    # `absolute_table_location` would be dead code that looks live: it PREFERS the described location over its argument,
    # so both sides come back identical and the guard would admit every 409 regardless of where it pointed.
    claimed = (registration.relative or "").strip("/")
    if not (registered and claimed and registration_points_at(registered, claimed)):
        return
    # A refused register whose detach failed also lands here on retry; a flag-256 dataset's
    # record is never converged, and a dataset that cannot be read is not judged clean.
    try:
        facts = await run_in_threadpool(registered_dataset_facts, registered, registration.so)
    except Exception as exc:
        log.warning("register_converge_unjudged", extra={"table": registration.id, "location": registered, "error": str(exc)[:300]})
        raise
    if facts is None:
        return
    if facts.mixed:
        log.warning("register_converge_refused_mixed_file_versions", extra={"table": registration.id, "location": registered})
        return
    if not facts.stable_row_ids:
        log.warning("register_converge_refused_unstable_row_ids", extra={"table": registration.id, "location": registered})
        return
    await _converge_the_base_record(registration, registered, facts)


async def _converge_the_base_record(registration: Registration, registered: str, facts: RegisteredDataset) -> None:
    """Record the bases of the dataset at ``registered`` and claim its data directories, or log why it records nothing."""
    # THE RECORD CONVERGES ([[LH-279]]). A writer that registers its output on every run re-enters
    # the register door each time, so a table registered before its record existed gets one here — judged
    # exactly as a fresh registration's bases are, and never for a base nothing sanctions.
    #
    # A registered clone's roots stand on the pins its record holds, which its tags keep, and only a registration
    # takes those tags (`table_bases.admit_registered_table`): a record naming a source whose version nothing
    # keeps is never written ([[LH-576]]). A clone left attached without its record is deregistered and
    # registered again.
    clone = await run_in_threadpool(partial(table_bases.clone_of, facts.clone, facts.bases, location=registered, context=registration.context))
    if clone.sources:
        log.warning("register_converge_refused_clone", extra={"table": registration.id, "location": registered})
        return
    unsanctioned = False
    entries: list[base_registry.RecordedBase] = []
    try:
        entries = await run_in_threadpool(
            partial(
                table_bases.entries_for_registration,
                registered,
                facts.bases,
                registry=registration.context.registry,
                configured=registration.context.configured,
                data_allowlist=registration.context.data_allowlist,
                clone=clone,
            )
        )
    except InvalidInputError:
        unsanctioned = True
    if unsanctioned:
        log.warning("register_converge_refused_unrecorded_bases", extra={"table": registration.id, "location": registered})
        return
    # [[LH-252]] A data directory another table holds is never converged into this one's record, and one is
    # claimed only for the uuid the location's claim records ([[LH-575]]).
    standing = await run_in_threadpool(location_claims.holder, table_claims.claim_store(registration.settings), registered)
    if standing is None or standing.uuid is None:
        log.warning("register_converge_refused_unidentified", extra={"table": registration.id, "location": registered})
        return
    claimant = location_claims.DirectoryClaimant(uuid=standing.uuid, root=registered, table=registration.context.holder, segments=registration.segments)
    held = False
    try:
        await run_in_threadpool(table_bases.claim_registered_directories, registration.context.registry, entries, claimant)
    except InvalidInputError:
        held = True
    if held:
        return
    await run_in_threadpool(base_registry.claim_bases, registration.context.registry, registered, entries)
    log.info("register_converged_base_record", extra={"table": registration.id, "location": registered})


async def deregister(registration: Registration) -> None:
    """Deregister the table this registration attached, leaving its bytes where they were found."""
    # DEREGISTER, never drop. Register ATTACHES bytes that already existed and are not ours to
    # destroy — the whole point of the door. Deregister is the exact inverse: it removes the
    # catalog object this request created and leaves the data exactly where it was found.
    await run_in_threadpool(native.call, registration.ns, "deregister_table", DeregisterTableRequest(id=registration.segments))


async def _release_the_location(registration: Registration, claimed_location: str) -> None:
    """Release the location's claim this registration took, and end the records kept under its uuid."""
    # The location's claim goes with the row; one another table holds is not this registration's to free.
    # The records kept under this request's uuid go last, once no table holds it.
    # Only the claim this registration took, under the uuid it minted: a take refused (another request's fence) took
    # none, and the claim standing there is another table's under the same name.
    await run_in_threadpool(partial(location_claims.release, registration.claims, claimed_location, registration.canonical, uuid=registration.table_uuid))
    settings = registration.settings
    await run_in_threadpool(object_records.end_object_records, settings.registry_root, settings.storage_options(), "table", uuid=registration.table_uuid)


async def detach_the_registration(registration: Registration, claimed_location: str) -> None:
    """Deregister the table and release its location's claim."""
    await deregister(registration)
    await _release_the_location(registration, claimed_location)


async def admit_the_registration(registration: Registration, claimed_location: str, asked: set[str]) -> _AdmittedDataset:
    """Mark the table's heads, take its location's claim, judge its dataset and admit it."""
    # The commit marks first, at each ref's head ([[LH-550]]): every version the dataset holds now was committed past
    # every door, and the claim is what makes the table one the commit reconciler reads.
    await run_in_threadpool(
        partial(commit_reconcile.mark_registered_heads, registration.settings, registration.so, location=claimed_location, table_uuid=registration.table_uuid)
    )
    # Then the location's claim ([[LH-204]]): a rename holding it, or a table a recoverable drop is keeping it for,
    # refuses this registration even when the manifest shows no other row at the location. A rename between its two
    # names holds the claim fenced ([[LH-586]]), and that refusal is a concurrent change (409, code 14): the location is
    # held by a table in flight, and the same registration is judged afresh once the rename has ended.
    try:
        await run_in_threadpool(
            partial(
                table_claims.claim_new_table,
                registration.ns,
                registration.claims,
                claimed_location,
                table=registration.canonical,
                segments=registration.segments,
                table_uuid=registration.table_uuid,
            )
        )
    except location_claims.LocationFencedError as fenced:
        log.warning("register_refused_fenced_location", extra={"table": registration.id, "location": fenced.claim.location, "holder": fenced.claim.table})
        raise ConcurrentModificationError(f"cannot register {registration.id}: another request is acting on the table at its location; retry") from None
    except location_claims.LocationHeldError as held:
        log.warning("register_refused_held_location", extra={"table": registration.id, "location": held.claim.location, "holder": held.claim.table})
        raise table_bases.occupied_location_refusal(registration.relative) from None
    # Holding the claim, so of this registration and the finish of an ending at the location, the second sees the first
    # (ADR 0198 §1): the clone that ended here may hold tags of the very names this one would take.
    await run_in_threadpool(clone_endings.refuse_registration_while_ending, registration.claims, claimed_location)
    verdict = await run_in_threadpool(
        table_bases.judge_registered_table,
        registration.ns,
        registration.so,
        registration.segments,
        relative=registration.relative,
        context=registration.context,
    )
    # The sources judged on the registered dataset are the ones its caller was asked for: one that differs (the source
    # changed hands meanwhile) was asked of no one, and is refused as one the caller may not tag.
    if unasked := [source for source in verdict.sources if source.table_uuid not in asked]:
        log.warning("register_refused_unasked_clone_source", extra={"table": registration.id, "sources": [source.table for source in unasked]})
        raise table_bases.refused_clone(unasked[0].location)
    admitted = await run_in_threadpool(
        partial(
            table_bases.admit_registered_table,
            registration.so,
            segments=registration.segments,
            verdict=verdict,
            clone_uuid=registration.table_uuid,
            context=registration.context,
        )
    )
    return _AdmittedDataset(verdict=verdict, admitted=admitted)


class _AdmittedDataset(BaseModel):
    """What admitting a registered dataset judged it, and what the admission took."""

    model_config = ConfigDict(frozen=True)

    verdict: table_bases.RegistrationVerdict
    admitted: table_bases.Admitted


async def undo_the_admitted_registration(registration: Registration, admission: Admission) -> None:
    """Deregister the admitted table, release what its admission took, then its location's claim."""
    # The row first: while the table is attached its record must stay, because its manifest still declares those
    # bases. Then the entries, directories and clone tags this registration took, and the location's claim last,
    # as at every other end of a clone: a tag the store keeps leaves the location held, so no clone registered
    # there next takes a tag of the same name while this one stands.
    await deregister(registration)
    await run_in_threadpool(
        partial(table_bases.release_admitted, admission.admitted, clone_location=admission.location, so=registration.so, context=registration.context)
    )
    await _release_the_location(registration, admission.claimed_location)


class DropToRestore(BaseModel):
    """The trash record an undrop restores, and the uuid it keeps."""

    model_config = ConfigDict(frozen=True)

    record: trash.TrashRecord
    uuid: str


async def take_the_location_back(ns: LanceNamespace, settings: Settings, location: str, *, segments: list[str], canonical: str, record_uuid: str) -> None:
    """Take back the claim on ``location`` the drop kept for the table under ``record_uuid``."""
    # The drop kept the location's claim for this id ([[LH-204]]), so this take is the retry of an
    # existing hold; another holder means another table now resolves to these bytes, and re-attaching
    # this id would put two live ids on one dataset.
    try:
        kept = await run_in_threadpool(table_claims.take, ns, table_claims.claim_store(settings), location, table=canonical, segments=segments)
    except location_claims.LocationFencedError as fenced:
        if fenced.claim.uuid != record_uuid:
            raise InvalidTableStateError(f"cannot undrop {canonical}: its location is now held by another table") from None
        # The fence is a request's on this very table: a drop of it settling, whose fence lapses within its lease.
        raise ConcurrentModificationError(f"cannot undrop {canonical}: a drop of this table is still settling; retry once its lease has passed") from None
    except location_claims.LocationHeldError as held:
        log.warning("undrop_refused_held_location", extra={"table": canonical, "location": held.claim.location, "holder": held.claim.table})
        raise InvalidTableStateError(f"cannot undrop {canonical}: its location is now held by another table") from None
    if kept.uuid != record_uuid:
        raise InternalError(f"the claim on {canonical}'s location records another uuid than its trash record, so its grants cannot be found")
