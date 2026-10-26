"""The tables derived from an erased one, each holding the obligation until an obligation of its own follows it ([[LH-332]]).

An erasure covers one table. A run that read it wrote rows into another table, which keeps them whatever the job does to
this one, and the source's predicate cannot judge that table: it may keep the subject under columns of its own, or only
as the ``source_rowid`` of the source's stable row ids (`lance_docs/lance/format/table/row_id_lineage.md` "Stable Row ID").
So the verification names every table the edge list records as derived from the erased one and holds the obligation open
until whoever may erase each records an obligation following it, with a predicate of its own (owner, 2026-10-20, ADR 0178).

**THE CATALOG READS THE EDGE LIST ITSELF** (ADR 0158: governance decisions read only what rask holds). It is lineage's
``provenance$edges``, a table of the catalog's provenance namespace, located through DescribeTable and opened in-process
as the commit reconciler opens ``provenance$events``, and walked as lineage's ``GET /tables/{name}/derived`` walks it
(:mod:`service_kit.lakehouse.derived_tables`), but to its last table: a table left unnamed is a subject forgotten.

A derived table is:

* ``followed`` while an obligation recorded on it follows this one, open or released, directly or through a chain
  (:func:`catalog.services.erasure_obligations.followed_tables`);
* ``failed`` while a location claim is held for it, live or recoverably dropped, and no obligation follows this one on
  it, which keeps the obligation open, as a key naming no table uuid does: an outside producer's event that named the
  table without one, which no obligation can be tied to;
* ``held`` once deregistered with its bytes still at its location: the claim is released, so no obligation can be
  recorded on it, and the bytes may hold the subject, so the obligation stays open until they are deleted;
* ``gone`` once no obligation can be recorded on it and nothing of it stands: its claim released and no dataset at its
  location, or no claim recording it any more, its location taken by another table since.

The claims are read whole (:func:`service_kit.lakehouse.location_claims.every_claim`): a claim that cannot be read may be
the table's own, which would otherwise pass as gone.

**WHAT THIS CANNOT PROMISE.** Lineage records a derived table's edges when it consumes the catalog's announcement of that
table's write, so a table written from this one while lineage is behind, or moments before the verification, is not
named yet, and a release is final. A deregistered table registered again at its location is a table of a new uuid that
no edge links to this one, and the claim it takes leaves the old uuid named by no claim, so it reads as gone. Edges an
outside producer keyed by a name the table held before a rename are not reached, as lineage's door does not reach them
either; and with authorization off, lineage admits a name-keyed output, which holds the obligation for good.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final

import lance
from lance_namespace import DescribeTableRequest, LanceNamespace, NamespaceNotFoundError, TableNotFoundError

from catalog.core.config import Settings
from catalog.services import erasure_obligations, native, table_claims
from catalog.services.erasure import SurfaceResult, derived_surface
from catalog.services.erasure_obligations import ErasureObligation
from service_kit.lakehouse import derived_tables, location_claims, objectfs
from service_kit.lakehouse.base_refs import key_uri, store_of
from service_kit.lakehouse.objectfs import StorageOptions


_FOLLOWED: Final = "an erasure obligation recorded on it follows this one, and judges it with a predicate of its own"
_DEREGISTERED: Final = (
    "deregistered, its bytes left at its location outside the catalog, where they may hold the subject: the obligation is held until they are deleted"
)
_DROPPED: Final = "it was dropped with its bytes, so nothing of it can hold the subject or an obligation"
_RELOCATED: Final = "no claim records it any more: another table took its location since, and may hold its bytes under a uuid no edge links to this one"
_UNIDENTIFIED: Final = "the edge list keys it by a name, not a table uuid, so no erasure obligation can be tied to it"


def judge(settings: Settings, ns: LanceNamespace | None, so: StorageOptions, *, obligation: ErasureObligation, table: str) -> list[SurfaceResult]:
    """Every table the edge list records as derived from ``obligation``'s table, nearest first, as a ``derived:<key>``
    surface each; none where the catalog keeps no edge list.

    ``ns`` is the connection holding the provenance namespace, ``None`` when the catalog keeps none. ``table`` is the erased
    table's name now, which an outside producer's event may have keyed its edges by.

    Raises:
        OSError: The edge list, a claim or a derived table's location could not be read.
        ValueError: A claim record is malformed.
        lance_namespace.LanceNamespaceError: The provenance namespace could not describe the edge list.

    The verification reports any of them as a failed ``derived`` surface, which keeps the obligation open.
    """
    edges = _edge_list(settings, ns, so)
    if edges is None:
        return []
    walked = derived_tables.walk(
        lambda where: edges.to_table(columns=list(derived_tables.COLUMNS), filter=where).to_pylist(), {obligation.table_uuid, table}, max_depth=None
    )
    ordered = sorted(walked.depths, key=lambda key: (walked.depths[key], key))
    if not ordered:
        return []
    followed = erasure_obligations.followed_tables(
        erasure_obligations.list_obligations(settings.registry_root, settings.storage_options()), obligation.obligation_id
    )
    unfollowed = [key for key in ordered if derived_tables.is_table_uuid(key) and key not in followed]
    held, released = _claimed(settings) if unfollowed else (frozenset(), {})
    store = store_of(settings.root)
    kept = frozenset(key for key in unfollowed if key not in held and key in released and objectfs.is_lance_dataset_root(key_uri(store, released[key]), so))
    return [
        _judged(key, walked.names.get(key, key), followed=followed, held=held, released=released.keys(), kept=kept, obligation_id=obligation.obligation_id)
        for key in ordered
    ]


def _edge_list(settings: Settings, ns: LanceNamespace | None, so: StorageOptions) -> lance.LanceDataset | None:
    """The edge list at its latest version; ``None`` where the catalog keeps no provenance namespace, or lineage has created
    no edge list in it, so no edge was ever recorded."""
    if ns is None:
        return None
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=[settings.provenance_namespace, derived_tables.EDGES]))
    except (NamespaceNotFoundError, TableNotFoundError):
        return None
    return lance.dataset(str(described.location), storage_options=dict(so) if so else None)


def _claimed(settings: Settings) -> tuple[frozenset[str], Mapping[str, str]]:
    """The uuid of every table a location claim is held for (a live table's, and a recoverably dropped one's whose undrop it
    keeps), and the store key of every other table a released claim still records."""
    held: set[str] = set()
    released: dict[str, str] = {}
    for claim in location_claims.every_claim(table_claims.claim_store(settings)):
        if claim.uuid is None:
            continue
        if claim.released_at is None:
            held.add(claim.uuid)
        else:
            released[claim.uuid] = claim.location
    return frozenset(held), {table_uuid: location for table_uuid, location in released.items() if table_uuid not in held}


def _judged(
    key: str, name: str, *, followed: Collection[str], held: Collection[str], released: Collection[str], kept: Collection[str], obligation_id: str
) -> SurfaceResult:
    """One derived table's surface; its detail names it as its newest edge was recorded, which reaches only a reader who may
    see the table (:func:`catalog.services.erasure.shown`)."""
    surface = derived_surface(key)
    if not derived_tables.is_table_uuid(key):
        return SurfaceResult(surface=surface, outcome="failed", detail=_UNIDENTIFIED)
    if key in followed:
        return SurfaceResult(surface=surface, outcome="followed", detail=f"{name} on the edge list: {_FOLLOWED}")
    if key in held:
        owed = (
            f"{name} on the edge list: no erasure obligation follows this one on it: whoever may erase it records one with a "
            f"predicate of its own, naming `follows`: {obligation_id} (undropping it first while it is in the trash)"
        )
        return SurfaceResult(surface=surface, outcome="failed", detail=owed)
    if key in kept:
        return SurfaceResult(surface=surface, outcome="held", detail=f"{name} on the edge list: {_DEREGISTERED}")
    return SurfaceResult(surface=surface, outcome="gone", detail=f"{name} on the edge list: {_DROPPED if key in released else _RELOCATED}")
