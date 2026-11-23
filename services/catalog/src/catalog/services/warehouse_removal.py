"""What a warehouse's delete does to the store and the catalog's records: which namespaces it holds, each table's
detach or destruction, the bytes a purge may delete, and the trash records it ends.

The door authorizes the caller, plans the delete, announces each step and ends grants; nothing here reads a request
or a grant.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from functools import partial

from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DeregisterTableRequest,
    DescribeTableRequest,
    DescribeTableResponse,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    TableNotFoundError,
)
from pydantic import BaseModel, ConfigDict

from catalog.core import native
from catalog.core.config import CatalogStore, Settings
from catalog.services import clone_endings, namespace_subtrees, table_claims
from catalog.services.clone_ending_records import CloneEnding
from catalog.services.governed_object import GovernedObject
from catalog.services.tables import destroy
from lakehouse.governed import fga_ids
from lakehouse.records import base_registry, clone_sources, data_directories, location_claims, trash
from lakehouse.records.location_claims import ClaimStore
from lakehouse.records.trash import TrashRecord
from lakehouse.records.warehouse_records import WarehouseRecord


log = logging.getLogger(__name__)


async def namespaces_in(ns: LanceNamespace, warehouse_id: str) -> list[str]:
    """The namespaces directly under warehouse ``warehouse_id``'s own name, as sorted ids: its contents for a delete.

    A warehouse whose row is gone holds none: a half-finished earlier delete dropped it last.
    """
    try:
        _response, names = await namespace_subtrees.drain_namespaces(ns, [warehouse_id])
    except NamespaceNotFoundError:
        return []
    return [fga_ids.canonical_object_id([warehouse_id, name]) for name in sorted(names)]


class NamespaceSubtree(BaseModel):
    """A namespace directly in the warehouse and what the catalog lists under it, enumerated before the cascade changes anything."""

    model_config = ConfigDict(frozen=True)

    segments: list[str]
    #: The namespace's uuid, ``None`` when the backend no longer has it.
    uuid: str | None
    descendants: list[GovernedObject]

    @property
    def tables(self) -> list[list[str]]:
        return [item.segments for item in self.descendants if item.kind == "table"]

    @property
    def namespaces(self) -> list[list[str]]:
        """Every namespace in the subtree, deepest first and its own last, so each is empty when its own drop runs."""
        nested = sorted((item.segments for item in self.descendants if item.kind == "namespace"), key=len, reverse=True)
        return [*nested, self.segments]

    @property
    def guarded(self) -> list[GovernedObject]:
        """Every object in the subtree whose protection the cascade is held to: its descendants and the namespace itself."""
        own = [GovernedObject(kind="namespace", segments=self.segments, uuid=self.uuid)] if self.uuid is not None else []
        return [*self.descendants, *own]


async def detach_table(ns: LanceNamespace, settings: CatalogStore, segments: list[str], *, purge: bool, table_uuid: str) -> bool:
    """Detach one table the cascade listed from the catalog, its bytes left in place; ``False`` for one it did not detach.

    DETACH, NOT DROP: the warehouse's delete takes bytes only on ``?purge_bucket=true``, and that holds for the tables in its
    namespaces as much as for the bucket. A detached table's bytes stay where a later ``register_table`` can attach
    them again; a purge takes them with the bucket, and then their base record goes with them ([[LH-279]]), or it
    would sanction its bases for the next table registered at the location. The location's claim goes either way,
    as at the deregister door ([[LH-204]]).

    A purge deletes the table's data directories BEFORE the deregister ([[LH-286]]): they are on approved bases, which
    the bucket purge does not reach, and the deregister is the step a re-issued call can no longer see past, so a delete
    that fails leaves the table registered for the re-issue to detach again.

    ``table_uuid`` is the uuid the cascade listed the table by. Its name is resolved again as its turn comes and its
    claim fenced, as the deregister door fences its table's (:func:`table_claims.fence_listed`, [[LH-586]]): a rename
    landing while the row goes answers 409 rather than carrying the table out of a cascade that then ends its grants,
    and a table that left since the listing, by a drop of its own or by a rename, is neither detached nor ended here.
    The fence is lifted when the detach fails with the row in place, and goes with the claim's release when the row went.

    Raises:
        ConcurrentModificationError: The table changed, or another request is acting on it; nothing of it changed here.
    """
    canonical = fga_ids.canonical_object_id(segments)
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    claims = table_claims.claim_store(settings)
    resolved = await run_in_threadpool(table_claims.resolve, ns, claims, segments)
    fenced = await run_in_threadpool(partial(table_claims.fence_listed, ns, claims, resolved, listed_uuid=table_uuid, verb="deregister"))
    if fenced is None:
        return False
    try:
        # A clone's ending is filed before anything changes (ADR 0198 §1), so a filing that fails refuses with every byte
        # and the row in place, and a tag release failing below is finished by maintenance's audit, where a re-issued
        # cascade would no longer list the clone it belongs to.
        ending = await run_in_threadpool(
            partial(
                clone_endings.file_ending,
                registry,
                claims,
                fenced.location,
                table=canonical,
                forget_record=purge,
                door="warehouse_cascade",
                filing=fenced.token,
            )
        )
        if purge:
            await _delete_table_directories(registry, claims, fenced, table_uuid=table_uuid, ending=ending, settings=settings)
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    except TableNotFoundError:
        # Only the deregister answers this: the row went by a hand the fence does not bind, and this ending's finish
        # releases what the clone held. The fence is lifted, as the deregister door lifts its own.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        return False
    except Exception:
        # A failed native call leaves the ending: a describe cannot tell whether the row it followed is still the
        # clone's, and the finish judges it on its own reads once the claim's lease has passed. The fence is lifted,
        # for the re-issue that finishes the cascade.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    # What the catalog promised a clone among the bytes ends before the location's claim does, as at the deregister
    # door: a tag that cannot be deleted leaves the location held, and no clone registered there can hold its name.
    if purge:
        await run_in_threadpool(partial(base_registry.forget_base_record, registry, fenced.location, uuid=table_uuid))
    else:
        await run_in_threadpool(partial(base_registry.release_clone_sources, registry, fenced.location, uuid=table_uuid))
    # Released under the uuid the cascade listed the table by, as well as under its fence: a purge's directory delete
    # can take longer than a fence stands, and the same name registered at the location once the fence has lapsed holds
    # the claim under a uuid of its own.
    await run_in_threadpool(partial(location_claims.release, claims, fenced.location, canonical, uuid=table_uuid))
    if ending is not None:
        await run_in_threadpool(clone_endings.clear_ending, claims, ending)
    return True


async def _delete_table_directories(
    registry: base_registry.BaseRegistry,
    claims: ClaimStore,
    fenced: table_claims.DoorFence,
    *,
    table_uuid: str,
    ending: CloneEnding | None,
    settings: CatalogStore,
) -> None:
    """Delete the data directories of the table ``fenced`` holds for a purge."""
    try:
        await run_in_threadpool(
            partial(data_directories.delete_table_directories, registry, fenced.location, uuid=table_uuid, storage_options=settings.storage_options())
        )
    except Exception:
        # A delete that fails releases the clone's ending before the failure is answered: this request has not touched
        # the row, so the clone's holds are its own still.
        if ending is not None:
            await run_in_threadpool(clone_endings.clear_ending, claims, ending)
        raise


async def require_no_outside_clones(ns: LanceNamespace | None, settings: CatalogStore, *, subtrees: list[NamespaceSubtree], trashed: list[TrashRecord]) -> None:
    """Refuse a ``?purge_bucket=true`` that would destroy a table a registered clone outside the warehouse reads, before
    anything changes (ADR 0198 §1): the purge empties the bucket, so a source's bytes go whatever its clones' tags say.

    Raises:
        InvalidTableStateError: A table in the warehouse, live or trashed, is read by a clone outside it (code 19, 409).
        ServiceUnavailableError: A table's tags could not be listed.
    """
    so = settings.storage_options()
    located = await _live_table_locations(ns, subtrees)
    for entry in trashed:
        if location := entry.location:
            located[f"{entry.id}@{entry.uuid}"] = location
    carried = {name: await run_in_threadpool(partial(destroy.clone_readers, location, so, table=name)) for name, location in located.items()}
    try:
        clone_sources.destroy_order({name: clone_sources.tag_of(location) for name, location in located.items()}, carried)
    except clone_sources.ReadFromOutsideError as exc:
        log.warning("warehouse_purge_refused_clone_sources", extra={"read": {name: sorted(tags) for name, tags in exc.read.items()}})
        raise InvalidTableStateError(
            f"the purge would destroy {len(exc.read)} table(s) read by registered clones outside this warehouse; drop or deregister those "
            "clones first, or delete the warehouse without ?purge_bucket=true"
        ) from exc


async def _live_table_locations(ns: LanceNamespace | None, subtrees: list[NamespaceSubtree]) -> dict[str, str]:
    """Where each table ``subtrees`` lists stands, by its canonical id."""
    located: dict[str, str] = {}
    # None for a warehouse whose store is not opened, and a table gone since it was listed is left out.
    if ns is None:
        return located
    for table in [table for subtree in subtrees for table in subtree.tables]:
        with suppress(TableNotFoundError):
            described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=table))
            if described.location:
                located[fga_ids.canonical_object_id(table)] = described.location
    return located


async def forget_trashed(settings: CatalogStore, entry: TrashRecord, *, purge: bool) -> None:
    """End what one trashed object kept for an undrop besides its grants, as the trash's own ending does
    (:func:`lakehouse.records.trash.end`): its records, a table's claim and base record, then its record.

    A purge deletes a trashed table's data directories before its base record ([[LH-286]]): they are on approved bases,
    which the bucket purge does not reach, and nothing names them once the record is gone. Without one the bytes stay,
    as the bucket does, where a later register can attach them again. A record that keeps no uuid is filed under none,
    so nothing of it can be ended.
    """
    if (uuid := entry.uuid) is None:
        return
    so = settings.storage_options()
    if purge and (location := entry.location):
        registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=so)
        await run_in_threadpool(partial(data_directories.delete_table_directories, registry, location, uuid=uuid, storage_options=so))
    await run_in_threadpool(partial(trash.end, settings.registry_root, so, entry, kind=entry.kind, uuid=uuid))


def require_bucket_purgeable(settings: Settings, records: list[WarehouseRecord], record: WarehouseRecord, *, warehouse_id: str) -> None:
    """Refuse a ``?purge_bucket=true`` that would destroy bytes this warehouse does not solely own.

    ``create_warehouse`` carries two bucket-claim guards, and NEITHER of them survives into the destroy
    path on its own:

    * A bucket may back TWO warehouses of the SAME project — the cross-claim guard subtracts the caller's
      own project on purpose, so one project may keep several warehouses on one bucket. A purge
      deletes every object AND the bucket, so deleting one of the pair would silently destroy the other's
      data and leave its record pointing at a bucket that no longer exists.
    * The reserved platform buckets (the catalog root and registry, the data bases) are refused at create — but a
      record written before that guard existed, or by anything else with registry write access, still names
      one, and purging it would take the whole estate's registry with it.

    ``NamespaceNotEmptyError`` (409) is the refusal: the container still holds something that is not this
    warehouse's to destroy. The reserved case is ``InvalidInputError`` (400), matching create's own wording
    for the same bucket. Both run BEFORE any irreversible step, so a refusal costs nothing.
    """
    bucket = record.bucket
    if bucket in settings.reserved_bucket_set:
        raise InvalidInputError(
            f"bucket {bucket!r} is reserved platform storage (the catalog root or registry, or a platform bucket) "
            f"and must not be purged — delete the warehouse without ?purge_bucket=true"
        )
    rivals = [r for r in records if r.bucket == bucket and r.id != warehouse_id]
    if not rivals:
        return
    # Name only SAME-project siblings. A cross-project claim is drift, and this caller cleared the bar for
    # THIS warehouse's project only — enumerating another tenant's warehouse ids is the disclosure the
    # create-side guard already declines to make.
    named = sorted(r.id for r in rivals if r.project == record.project)
    holder = f"warehouse(s) {', '.join(named)}" if len(named) == len(rivals) else "another project's warehouse"
    raise NamespaceNotEmptyError(
        f"bucket {bucket!r} still backs {holder}, whose data the purge would destroy. "
        f"Delete this warehouse without ?purge_bucket=true, or delete the other warehouse(s) first."
    )
