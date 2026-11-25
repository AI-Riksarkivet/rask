"""A name to the FGA object its grants are held by.

A table or namespace is asked about as the object its grants are held by, ``table:<uuid>`` or ``namespace:<uuid>``
(ADR 0173 §8, [[LH-524]]): every door is addressed by name, and the name is resolved once per request
(``catalog.api.dependencies.Resolver``), so the door acts on the object it authorized.
"""

import asyncio
import logging
from collections.abc import Sequence
from typing import Literal

from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespace, NamespaceNotFoundError

from catalog.api.authz.rungs import TRASH_DOORS
from catalog.api.dependencies import Resolver
from catalog.core import write_judge
from catalog.core.config import CatalogStore
from catalog.services import object_uuids, table_claims
from catalog.services.governed_object import GovernedObject
from lakehouse.governed import fga_ids
from lakehouse.governed.settings import FgaSettings
from lakehouse.problems.outage import store_refusal
from lakehouse.records import trash
from lakehouse.records.trash import TrashKind


__all__ = [
    "GovernedObject",
    "addressed_object",
    "identify_descendants",
    "identify_namespace",
    "listed_object",
    "namespace_parent",
    "owned_object",
    "parent_object_of",
    "resolve_rung",
    "rung_label",
    "rung_on",
    "standing",
    "standing_ancestors",
    "trashed_at_or_above",
    "trashed_objects",
]


log = logging.getLogger(__name__)


def addressed_object(fga_type: str, id_segments: list[str] | str) -> str:
    """``<type>:<canonical-id>`` for an object addressed by its id.

    The FGA object of a type no name is minted a uuid for (a transaction's opaque id, a classification label, a
    materialized view), and the NAME of a table or namespace in an audit record or a refusal. A table's or namespace's
    grants are held by its uuid (:func:`_named`), never by this.
    """
    return f"{fga_type}:{fga_ids.canonical_object_id(id_segments)}"


def _named(fga_type: str, segments: Sequence[str], *, in_trash: bool = False) -> write_judge.Named | None:
    """``fga_type`` ``segments`` as a name its grants' object is resolved from, or ``None`` for a type no uuid keys."""
    if fga_type == "table":
        return write_judge.Named(kind="table", segments=tuple(segments), in_trash=in_trash)
    if fga_type == "namespace":
        return write_judge.Named(kind="namespace", segments=tuple(segments), in_trash=in_trash)
    return None


def rung_on(relation: str, fga_type: str, segments: Sequence[str], *, suffix: str = "") -> write_judge.Rung:
    """``relation`` on ``fga_type`` ``segments``: on the name a table or namespace is resolved from, else on the id's object.

    A trash door's name also resolves to the object the trash holds under it (:data:`TRASH_DOORS`).
    """
    named = _named(fga_type, segments, in_trash=suffix in TRASH_DOORS)
    return write_judge.Rung(relation=relation, on=named if named is not None else addressed_object(fga_type, list(segments)))


def rung_label(rung: write_judge.Rung) -> str:
    """The object ``rung`` is asked on, by name: what a refusal and the audit of an absent object say.

    One spelling for a table that is there and one that is not, so a refusal does not tell a caller which names exist.
    """
    if isinstance(rung.on, str):
        return rung.on
    return addressed_object(rung.on.kind, list(rung.on.segments))


async def resolve_rung(resolver: Resolver, rung: write_judge.Rung) -> str | None:
    """The FGA object ``rung`` is asked on: what its name resolves to, ``None`` when nothing has that name.

    A trash door's name with no live object resolves to the one trashed object it names; several are asked one by one
    (:func:`require_trashed`).
    """
    if isinstance(rung.on, str):
        return rung.on
    obj = await resolver.object(rung.on.kind, list(rung.on.segments))
    if obj is None and rung.on.in_trash:
        trashed = await resolver.trashed(rung.on.kind, list(rung.on.segments))
        if len(trashed) == 1 and (uuid := trashed[0].uuid) is not None:
            return fga_ids.object_of(rung.on.kind, uuid)
    return obj


async def standing_ancestors(resolver: Resolver, segments: list[str]) -> list[str]:
    """The object of every ancestor namespace of ``segments`` that stands.

    An ancestor no uuid identifies raises, as it does at every door that resolves it ([[LH-534]]).
    """
    standing: list[str] = []
    for depth in range(1, len(segments)):
        if (ancestor := await resolver.object("namespace", segments[:depth])) is not None:
            standing.append(ancestor)
    return standing


async def trashed_at_or_above(resolver: Resolver, fga_type: str, segments: list[str]) -> list[str]:
    """The objects a recoverable drop trashed at ``segments`` or at an ancestor's name, whatever stands there now.

    What stands is not asked: a walk anchored at a standing namespace the caller cannot see would admit a trashed grant
    where none stands and refuse it under a namespace created there since, and so tell that namespace from an absent one
    ([[LH-236]]). The trash doors list a name's records the same way.
    """
    objects: list[str] = []
    for depth in range(1, len(segments) + 1):
        if depth == len(segments) and fga_type not in ("table", "namespace"):
            continue  # the trash keeps tables and namespaces, so no other type was trashed under its own name
        kind: TrashKind = "namespace" if depth < len(segments) or fga_type == "namespace" else "table"
        trashed = {uuid for record in await resolver.trashed(kind, segments[:depth]) if (uuid := record.uuid) is not None}
        objects += [fga_ids.object_of(kind, uuid) for uuid in sorted(trashed)]
    return objects


async def listed_object(resolver: Resolver, kind: Literal["table", "namespace"], name: str, *, segments: list[str]) -> str | None:
    """The FGA object a listing's candidate ``name`` resolves to; ``None`` for one that names nothing or records no uuid."""
    try:
        return await resolver.object(kind, segments)
    except object_uuids.UnidentifiedObjectError as unidentified:
        log.error("listing_left_out_unidentified_object", extra={"kind": kind, "object_name": name, "location": unidentified.location})
        return None


async def identify_namespace(resolver: Resolver, ns: LanceNamespace, segments: list[str]) -> GovernedObject:
    """The namespace ``segments`` names as this request resolved it, with its uuid: the one the router authorized, so a
    door keeps every record under the uuid its caller was judged on (ADR 0173 §7), as a table door does through
    ``ResolvedTableDep``; ``ns``, the door's connection, is read only where the request resolved no such name yet. A
    door that acts on the namespace by name asks :func:`require_namespace_as_identified` first.

    Raises:
        NamespaceNotFoundError: No such namespace.
        UnidentifiedObjectError: The namespace carries no uuid, which no door leaves.
    """
    uuid = await resolver.namespace_uuid(segments, ns=ns)
    if uuid is None:
        raise NamespaceNotFoundError(f"namespace not found: {segments}")
    return GovernedObject(kind="namespace", segments=segments, uuid=uuid)


async def standing(ns: LanceNamespace, settings: CatalogStore, kind: Literal["table", "namespace"], *, segments: list[str]) -> GovernedObject:
    """The object ``segments`` names now, read afresh: for a walker enumerating what lies under the container its
    request was authorized on, and for a door confirming after it acted that the name still holds its object. A door
    acting on the object its request names reads it as the request resolved it: :func:`identify_namespace`, or
    ``ResolvedTableDep`` for a table.

    Raises:
        TableNotFoundError: No such table.
        NamespaceNotFoundError: No such namespace.
        UnidentifiedObjectError: The object carries no uuid, which no door leaves.
    """
    if kind == "table":
        resolved = await run_in_threadpool(table_claims.resolve, ns, table_claims.claim_store(settings), segments)
        return GovernedObject(kind=kind, segments=segments, uuid=resolved.require_uuid(), location=resolved.location or "")
    return GovernedObject(kind=kind, segments=segments, uuid=await run_in_threadpool(object_uuids.require_namespace_uuid, ns, segments))


async def identify_descendants(ns: LanceNamespace, settings: CatalogStore, descendants: list[tuple[str, list[str]]]) -> list[GovernedObject]:
    """Every descendant a cascade is about to remove, with its uuid, read while it still stands.

    CONCURRENT (catalog-api-11): one independent read per descendant on the pre-flight of a cascade, so its latency
    does not grow with the subtree. Enumeration order is kept, so a refusal names the FIRST protected descendant.
    """
    return list(
        await asyncio.gather(*(standing(ns, settings, "table" if resource == "table" else "namespace", segments=child) for resource, child in descendants))
    )


# --------------------------------------------------------------------------- #
# Post-create ownership seeding — the single write-side authz policy. Every create
# endpoint calls this instead of inlining the guard + grant_on_create + parent_object.
# --------------------------------------------------------------------------- #


def owned_object(resource: str, uuid: str) -> str:
    """The FGA object a just-created ``resource`` with ``uuid`` is seeded on ([[LH-524]])."""
    return fga_ids.object_of("table" if resource == "table" else "namespace", uuid)


async def parent_object_of(resolver: Resolver, settings: FgaSettings, resource: str, *, segments: list[str]) -> str | None:
    """The object a just-created child's ``parent`` edge names: the object its parent's name is governed as
    (:meth:`Resolver.object`), ``warehouse:<id>`` for a name directly in a warehouse and the parent namespace's uuid
    object below it ([[LH-593]]).

    A name of one segment has no parent to name. No door creates one (:func:`_require_a_name_inside_a_warehouse`), and
    it is answered the estate's warehouse object for a namespace and ``None`` for a table (``table.parent`` accepts only
    a namespace), which :func:`require_parent` refuses.

    Raises:
        NamespaceNotFoundError: The parent namespace is gone; the create door checked it existed before its write.
    """
    if len(segments) > 1:
        parent = await resolver.object("namespace", segments[:-1])
        if parent is None:
            raise NamespaceNotFoundError(f"namespace not found: {segments[:-1]}")
        return parent
    return settings.fga_default_warehouse_object if resource == "namespace" else None


async def namespace_parent(resolver: Resolver, settings: FgaSettings, segments: list[str]) -> str:
    """:func:`parent_object_of` for a namespace, which always names one: its warehouse, or the namespace above it."""
    parent = await parent_object_of(resolver, settings, "namespace", segments=segments)
    if parent is None:
        raise NamespaceNotFoundError(f"namespace not found: {segments[:-1]}")
    return parent


def trashed_objects(settings: CatalogStore) -> set[str]:
    """The FGA objects of every table and namespace in the trash, by the uuid each record keeps.

    Read whole ([[LH-572]]): a record that cannot be read may be a trashed object of the subtree, whose grants and
    records stay for its undrop, so the walk refuses rather than ending them.

    Raises:
        ServiceUnavailableError: The store did not answer a read of the trash.
        InternalError: A trash record is there and cannot be read as one.
    """
    try:
        records = trash.every_record(settings.registry_root, settings.storage_options())
    except (OSError, ValueError) as exc:
        raise store_refusal(exc, reading="reading the trash", so="no object found only by its container's edge was ended") from exc
    objects: set[str] = set()
    for record in records:
        if (uuid := record.uuid) is not None:
            objects.add(fga_ids.object_of(str(record.kind or "table"), uuid))
    return objects
