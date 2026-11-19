"""What the trash doors share: how long a drop stays recoverable, which trashed object an undrop restores, and which
record no undrop restores. Where it re-attaches a table is ``dataplane.relative_location``, the rule every
re-registration shares.

A trash door stays addressed by name (ADR 0173 §9, [[LH-525]]). A name is free again the moment its object is
dropped, so several trashed objects may have held one, each its own uuid with its own grants and records: an undrop
restores exactly the one its caller names, and asks which when the name leads to several.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from functools import partial
from typing import Any

from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InternalError,
    InvalidTableStateError,
    LanceNamespace,
    RegisterTableRequest,
    RegisterTableResponse,
    TableAlreadyExistsError,
)

from catalog.core import write_judge
from catalog.core.config import Settings
from catalog.core.namespace import warn_if_mixed_file_versions
from catalog.schemas import TrashEntry
from catalog.services import dataplane, native, object_uuids, table_claims, warehouses
from lakehouse import location_claims, trash
from lakehouse.base_refs import decoded_path
from lakehouse.governed import fga


log = logging.getLogger(__name__)


def entry(record: dict[str, Any]) -> TrashEntry:
    """What a trash door answers about ``record``: the object, the name it held, its deadline and which side of it.

    ``expired`` reads the deadline by :func:`trash.past_deadline`, the rule the undrop refuses by and maintenance releases
    the grants by (ADR 0146), so a listing never calls a drop recoverable that the undrop refuses.
    """
    return TrashEntry(
        kind=trash.kind_of(record),
        **{field: str(record.get(field) or "") for field in ("id", "uuid", "location", "dropped_by", "dropped_at", "expires_at")},
        expired=trash.past_deadline(record),
    )


def listing_key(record: dict[str, Any]) -> str:
    """Where ``record`` sorts in a trash listing, and the page token that resumes after it: its name, then its uuid.

    A name can lead to several trashed objects, so the name alone is no cursor; the uuid makes each key one object's. No
    name holds a control character or an edge space (``identifiers.require_names``), so the space parts the two.
    """
    return f"{record.get('id') or ''} {trash.object_uuid(record) or ''}"


async def drop_grace_days(settings: Settings, segments: list[str]) -> float:
    """The days a recoverable drop of the object ``segments`` names stays recoverable, 0 for a destroying drop.

    Its warehouse's delete profile, else the estate's grace period (ADR 0146), read at the drop: the trash record stamps
    the deadline, so a profile changed later shortens no window already open.
    """
    return await run_in_threadpool(
        partial(
            warehouses.grace_days_under,
            settings.registry_root,
            settings.storage_options(),
            segments,
            estate=settings.trash_grace_days,
        )
    )


def the_one_to_restore(records: list[dict[str, Any]], *, kind: str, name: str) -> dict[str, Any]:
    """The one trash record among ``records`` an undrop of ``name`` restores.

    Raises:
        InvalidTableStateError: Several trashed objects held the name; the answer lists each one's uuid and deadline,
            and ``?uuid=`` names one.
    """
    if len(records) == 1:
        return records[0]
    listed = "; ".join(f"{trash.object_uuid(record)} (dropped {record.get('dropped_at')}, recoverable until {record.get('expires_at')})" for record in records)
    raise InvalidTableStateError(f"{len(records)} dropped {kind}s held the name {name!r}: {listed}. Name the one to undrop with ?uuid=<uuid>.")


def placing_back(ns: LanceNamespace, settings: Settings, *, segments: list[str], name: str) -> AbstractAsyncContextManager[None]:
    """The hold an undrop places the object ``segments`` names back under: its parent namespace's add fence, taken and the
    parent's run rule read as every placement's are (:func:`write_judge.placing`), kept until the block ends.

    An undrop is a placement like a create, a register or a rename in (ADR 0244): the namespace it restores into can be
    dropped, empty, and made afresh under the run rule, and only the parent's add fence keeps that from happening between
    the undrop's judgement and its register. Inside the hold :func:`require_its_own_parent` reads which namespace that is.
    No rule refuses an undrop: what it restores stood in that namespace under the rule the namespace holds all its life.

    Raises:
        NamespaceFencedError: The parent is being dropped, or made afresh by an Overwrite (409, code 14).
        NamespaceNotFoundError: The parent does not stand (404, code 1).
    """
    placed = write_judge.Write(op="undrop", obj=name, rung=None, table=tuple(segments))
    return write_judge.placing(placed, ns, control_root=settings.registry_root, storage_options=settings.storage_options())


async def require_its_own_parent(ns: LanceNamespace, record: dict[str, Any], *, segments: list[str], name: str) -> None:
    """Refuse restoring ``record`` under ``segments`` into a namespace other than the one it was dropped from; called
    inside :func:`placing_back`, so the namespace read is the one the object is placed in.

    A record keeps its namespace's uuid at the drop. Once that namespace is dropped too, its name is free, and another
    namespace created there is another uuid with its own owner and perhaps the run rule: restoring into it would attach
    the trashed object under a namespace its caller was never granted a create on, while its grants still name its own,
    and stand data no run produced under a rule. The uuid is read from the backend here, never from a request's cache of
    an earlier read. A record that names no parent (a table directly in a warehouse) has none to judge.

    Raises:
        InvalidTableStateError: The live namespace at the parent's name is not the one the object was dropped from.
    """
    dropped_from = record.get("namespace_uuid")
    if len(segments) < 3 or not isinstance(dropped_from, str):
        return
    standing = await run_in_threadpool(object_uuids.resolve_namespace_uuid, ns, segments[:-1])
    if standing != dropped_from:
        raise InvalidTableStateError(f"cannot undrop {name!r}: the namespace it was dropped from is gone, and another namespace holds its name now")


def require_restorable(record: dict[str, Any], *, name: str) -> None:
    """Refuse restoring a record whose drop is final: a residue record, or one past its deadline.

    A residue record names bytes a destructive drop that failed part-way left: its table was destroyed, not trashed,
    its row is gone and some of its objects may be too, so nothing an undrop re-attaches there is the table that was
    dropped ([[LH-532]]). Past ``expires_at`` the drop is DropTable's, its data deleted asynchronously by the purge, so
    no undrop restores it even while its bytes stand (ADR 0146).

    Raises:
        InvalidTableStateError: ``record`` is a residue record, or its grace period has ended.
    """
    if trash.is_residue(record):
        raise InvalidTableStateError(
            f"cannot undrop {name!r}: its bytes were left by a destructive drop that failed part-way, not kept for an undrop; the trash purge reclaims them"
        )
    if trash.past_deadline(record) or trash.is_released(record):
        raise InvalidTableStateError(
            f"cannot undrop {name!r}: its grace period ended at {record.get('expires_at')}, so the drop is final and the trash purge reclaims its bytes"
        )


async def restore_kept_table(
    ns: LanceNamespace,
    settings: Settings,
    record: dict[str, Any],
    *,
    segments: list[str],
    root: str,
    before_clear: Callable[[], Awaitable[None]],
) -> RegisterTableResponse:
    """Register again the trashed table ``record`` keeps, under ``segments``, and clear its record: the one step every
    undrop restores a table by, the table door's and the namespace undrop's for each table its cascade filed (ADR 0227).

    THE STEP IS A PLACEMENT (ADR 0244): it holds the add fence of the namespace the table goes back into from before it
    reads which namespace that is until its record is cleared (:func:`placing_back`, :func:`require_its_own_parent`), so
    the namespace it judged is the one it registers into, whichever door runs it. That fence is taken first and the
    claim's second, the order every request holding both takes them in (ADR 0244 § Lock order).

    THE STEP HOLDS THE TABLE'S CLAIM FENCED FROM BEFORE ITS REGISTER UNTIL ITS RECORD IS CLEARED. The drop kept the
    location's claim for this uuid ([[LH-204]]); fencing it under the ETag read (:func:`table_claims.fence_kept`) refuses
    a released claim or another holder, and while the fence stands no destroying drop deletes the bytes the register
    attaches, no recoverable drop or cascade files the record the clear deletes, and no create writes there. Under the
    fence the record is read again and a TableAlreadyExists judged (:func:`_register_kept`). ``before_clear`` runs once
    the table is registered and before its record is cleared (the door's announcement), so a failed clear, retried,
    announces again under the ids already filed.

    ``register_table`` REFUSES an absolute URI and keeps a relative one as the literal key it is given, so the table
    re-attaches at the key the record's URL decodes to under the connection's ``root`` ([[LH-517]]).

    Raises:
        InternalError: The record keeps no uuid, which no drop leaves.
        ConcurrentModificationError: Another request acts on the table, or its record changed since it was admitted.
        NamespaceFencedError: The namespace it goes back into is being dropped, or made afresh (409, code 14).
        NamespaceNotFoundError: That namespace does not stand (404, code 1).
        InvalidTableStateError: Another namespace than the one it was dropped from holds that namespace's name.
        KeptClaimRefusedError: The trashed table's location is no longer kept for it (code 19).
        TableAlreadyExistsError: Another table holds the name; the record stays for an undrop once the name is free.
    """
    canonical = fga.canonical_object_id(segments)
    record_uuid = trash.object_uuid(record)
    if record_uuid is None:
        raise InternalError(f"the trash record of {canonical} keeps no uuid, so its grants cannot be found")
    location = str(record["location"])
    body = RegisterTableRequest(id=segments, location=dataplane.relative_location(location, root=root))
    so = settings.storage_options()
    claims = table_claims.claim_store(settings)
    async with placing_back(ns, settings, segments=segments, name=canonical):
        await require_its_own_parent(ns, record, segments=segments, name=canonical)
        fenced = await run_in_threadpool(partial(table_claims.fence_kept, ns, claims, location, holder=canonical, segments=segments, uuid=record_uuid))
        try:
            response = await run_in_threadpool(partial(_register_kept, ns, claims, settings, body, record, segments=segments, location=location))
            await run_in_threadpool(warn_if_mixed_file_versions, location, so, table=canonical)
            await before_clear()
            # Under the fence, so the record cleared is the one this undrop admitted: no drop files another while it stands.
            await run_in_threadpool(trash.clear, settings.registry_root, so, record_uuid)
        finally:
            await run_in_threadpool(table_claims.lift_fence, claims, fenced)
    return response


def _register_kept(
    ns: LanceNamespace,
    claims: location_claims.ClaimStore,
    settings: Settings,
    body: RegisterTableRequest,
    admitted: dict[str, Any],
    *,
    segments: list[str],
    location: str,
) -> RegisterTableResponse:
    """Register the trashed table ``admitted`` keeps under ``segments`` at ``location``, under the undrop's fence (ADR 0227).

    The record is read again first: the one the undrop admitted, still there and kept for an undrop. A destroying drop
    that fenced the claim before the undrop did and ended without lifting it filed its residue record over it, and may
    have deleted the bytes. A RegisterTable answering TableAlreadyExists is this undrop's own registration only while
    the name describes this table, its uuid at its location: a recoverable drop whose deregister failed after its record
    left the table registered, or a namespace undrop issued again after its register, and the undrop converges on it
    (`undrop_table_already_registered`). Any other table under the name is the conflict, and the record stays for an
    undrop once the name is free.

    Raises:
        ConcurrentModificationError: The record is no longer the one admitted.
        TableAlreadyExistsError: Another table holds the name.
    """
    canonical = fga.canonical_object_id(segments)
    record_uuid = str(admitted["uuid"])
    now = trash.get(settings.registry_root, settings.storage_options(), record_uuid)
    if now is None or trash.is_residue(now) or now.get("dropped_at") != admitted.get("dropped_at"):
        raise ConcurrentModificationError(f"cannot undrop {canonical}: its trash record changed while this request was being served; describe it and retry")
    try:
        registered: RegisterTableResponse = native.call(ns, "register_table", body)
    except TableAlreadyExistsError:
        standing = table_claims.resolve(ns, claims, segments)
        if standing.uuid != record_uuid or standing.location is None or decoded_path(standing.location) != decoded_path(location):
            log.warning("undrop_refused_name_taken", extra={"table": canonical, "location": location, "standing": standing.location})
            raise TableAlreadyExistsError(f"cannot undrop {canonical}: another table took the name while this request was being served") from None
        log.info("undrop_table_already_registered", extra={"table": canonical})
        return RegisterTableResponse(location=location)
    return registered
