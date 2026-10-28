"""The tables derived from an erased one, each holding the obligation until an obligation of its own follows it ([[LH-332]]).

An erasure covers one table. A run that read it wrote rows into another table, which keeps them whatever the job does to
this one, and the source's predicate cannot judge that table: it may keep the subject under columns of its own, or only
as the ``source_rowid`` of the source's stable row ids (`lance_docs/lance/format/table/row_id_lineage.md` "Stable Row ID").
So the verification names every table derived from the erased one and holds the obligation open until whoever may erase
each records an obligation following it, with a predicate of its own (owner, 2026-10-20, ADR 0178).

**A TABLE IS DERIVED WHEN A RUN WRITING IT DECLARED THE SOURCE, OR A TABLE DERIVED FROM IT** (ADR 0183). Two records name
such a table, and the verification walks both together, following chains through either:

* the catalog's own input record (:mod:`catalog.services.table_inputs`), written before each declaring commit is
  finalized, so a table is named the moment it is committed, whether lineage is up or not;
* lineage's edge list ``provenance$edges``, a table of the catalog's provenance namespace, located through DescribeTable
  and opened in-process as the commit reconciler opens ``provenance$events`` (ADR 0158: governance decisions read only
  what rask holds), which also holds what an outside producer's events asserted.

Both are walked as lineage's ``GET /tables/{name}/derived`` walks its edges (:mod:`service_kit.lakehouse.derived_tables`),
but to the last table: a table left unnamed is a subject forgotten.

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

**WHAT THIS CANNOT PROMISE.** A write that declares no inputs derives from nothing, so a run that read this table and said
not so is not reached. A release is final: a table the walk reaches only after it (an outside producer's event lineage
records late) is a late derived table, which the commit reconciler's tick lists on the obligation and pages
(:func:`watch_released`). A deregistered table registered again at its location is a table of a new uuid that
no edge links to this one, and the claim it takes leaves the old uuid named by no claim, so it reads as gone. Edges an
outside producer keyed by a name the table held before a rename are not reached, as lineage's door does not reach them
either; and with authorization off, lineage admits a name-keyed output, which holds the obligation for good.

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from functools import cache, partial
from typing import Final

import lance
from lance_namespace import DescribeTableRequest, LanceNamespace, NamespaceNotFoundError, TableNotFoundError
from pydantic import BaseModel, ConfigDict

from catalog.core.config import Settings
from catalog.services import erasure_obligations, native, table_claims, table_inputs
from catalog.services.erasure import OPEN_OUTCOMES, SurfaceResult, derived_key, derived_surface
from catalog.services.erasure_obligations import ErasureObligation
from service_kit.lakehouse import derived_tables, location_claims, objectfs
from service_kit.lakehouse.base_refs import key_uri, store_of
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lancekit.filters import in_lists


_FOLLOWED: Final = "an erasure obligation recorded on it follows this one, and judges it with a predicate of its own"
_DEREGISTERED: Final = (
    "deregistered, its bytes left at its location outside the catalog, where they may hold the subject: the obligation is held until they are deleted"
)
_DROPPED: Final = "it was dropped with its bytes, so nothing of it can hold the subject or an obligation"
_RELOCATED: Final = "no claim records it any more: another table took its location since, and may hold its bytes under a uuid no edge links to this one"
_UNIDENTIFIED: Final = "the edge list keys it by a name, not a table uuid, so no erasure obligation can be tied to it"


def judge(settings: Settings, ns: LanceNamespace | None, so: StorageOptions, *, obligation: ErasureObligation, table: str) -> list[SurfaceResult]:
    """Every table derived from ``obligation``'s table, nearest first, as a ``derived:<key>`` surface each.

    ``ns`` is the connection holding the provenance namespace, ``None`` when the catalog keeps none, and then only the input
    record is walked. ``table`` is the erased table's name now, which an outside producer's event may have keyed its edges
    by.

    Raises:
        OSError: The input record, the edge list, a claim or a derived table's location could not be read.
        ValueError: An input record or a claim record is malformed.
        lance_namespace.LanceNamespaceError: The provenance namespace could not describe the edge list.

    The verification reports any of them as a failed ``derived`` surface, which keeps the obligation open.
    """
    records = table_inputs.every_record(settings.registry_root, settings.storage_options())
    derivation = _Derivation.walk(records, _edge_list(settings, ns, so), sources={obligation.table_uuid, table})
    obligations = erasure_obligations.list_obligations(settings.registry_root, settings.storage_options())
    return _judged_walk(settings, so, derivation, obligation=obligation, obligations=obligations, claimed=partial(_claimed, settings))


class LateDerived(BaseModel):
    """The late derived tables one tick found for one released obligation."""

    obligation_id: str
    tables: list[str]


def watch_released(settings: Settings, ns: LanceNamespace | None, so: StorageOptions) -> list[LateDerived]:
    """List on each released obligation every late derived table found since (ADR 0183 §5), and answer what was new.

    A late derived table is one derived from the obligation's table that no obligation follows the released one on and
    that still holds something of it (``failed`` or ``held``, as :func:`judge` judges it), whose write was declared before
    the release: the input record's first declaration of it, or the run time its edge names, came first. One whose edge
    names no readable time is listed, since nothing says it came after. A release is final, so such a table is listed for
    whoever may erase it to record an obligation following this one; a table written after the release read what the
    erasure had already removed. The source is keyed by its uuid and the name the obligation was recorded under.

    Raises:
        OSError: The obligations, the input record, the edge list or a claim could not be read.
        ValueError: An input record or a claim record is malformed.
        lance_namespace.LanceNamespaceError: The provenance namespace could not describe the edge list.
    """
    obligations = erasure_obligations.list_obligations(settings.registry_root, settings.storage_options())
    released = [o for o in obligations if o.released_at is not None]
    if not released:
        return []
    records = table_inputs.every_record(settings.registry_root, settings.storage_options())
    edges = _edge_list(settings, ns, so)
    # One reading of the claims for every obligation this tick judges, and only once one needs them.
    claimed = cache(partial(_claimed, settings))
    found: list[LateDerived] = []
    for obligation in released:
        derivation = _Derivation.walk(records, edges, sources={obligation.table_uuid, obligation.table})
        late = _late(settings, so, derivation, obligation=obligation, obligations=obligations, claimed=claimed)
        if late and (added := erasure_obligations.list_late_derived(settings.registry_root, settings.storage_options(), obligation.obligation_id, late)):
            found.append(LateDerived(obligation_id=obligation.obligation_id, tables=added))
    return found


def _late(
    settings: Settings,
    so: StorageOptions,
    derivation: _Derivation,
    *,
    obligation: ErasureObligation,
    obligations: Sequence[ErasureObligation],
    claimed: _Claimed,
) -> list[str]:
    released_at = obligation.released_at
    if released_at is None:
        return []
    owed = {
        key
        for surface in _judged_walk(settings, so, derivation, obligation=obligation, obligations=obligations, claimed=claimed)
        if surface.outcome in OPEN_OUTCOMES and (key := derived_key(surface.surface)) is not None and derived_tables.is_table_uuid(key)
    } - set(obligation.late_derived)
    return sorted(key for key, at in derivation.first_declared(owed).items() if at is None or at < released_at)


type _Claimed = Callable[[], tuple[frozenset[str], Mapping[str, str]]]


def _judged_walk(
    settings: Settings,
    so: StorageOptions,
    derivation: _Derivation,
    *,
    obligation: ErasureObligation,
    obligations: Sequence[ErasureObligation],
    claimed: _Claimed,
) -> list[SurfaceResult]:
    """Each table ``derivation`` reached, judged against every obligation recorded and the claims ``claimed`` reads."""
    walked = derivation.walked
    ordered = sorted(walked.depths, key=lambda key: (walked.depths[key], key))
    if not ordered:
        return []
    followed = erasure_obligations.followed_tables(obligations, obligation.obligation_id)
    unfollowed = [key for key in ordered if derived_tables.is_table_uuid(key) and key not in followed]
    held, released = claimed() if unfollowed else (frozenset(), {})
    store = store_of(settings.root)
    kept = frozenset(key for key in unfollowed if key not in held and key in released and objectfs.is_lance_dataset_root(key_uri(store, released[key]), so))
    return [
        _judged(key, walked.names.get(key, key), followed=followed, held=held, released=released.keys(), kept=kept, obligation_id=obligation.obligation_id)
        for key in ordered
    ]


class _Derivation(BaseModel):
    """What one reading of the input record and the edge list derived from one table."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    #: The source's keys: its uuid, and the name an outside producer's events may have keyed it by.
    sources: frozenset[str]
    walked: derived_tables.DerivedWalk
    records: list[table_inputs.TableInputs]
    edges: lance.LanceDataset | None

    @classmethod
    def walk(cls, records: Sequence[table_inputs.TableInputs], edges: lance.LanceDataset | None, *, sources: set[str]) -> _Derivation:
        """Walk ``records`` and ``edges`` together from the table ``sources`` key."""
        rows = [table_inputs.as_edges(records)]
        if edges is not None:
            # A dataset handle reads the version it opened, so every hop of the walk reads one version of the edge list.
            rows.append(derived_tables.filtered(lambda where: edges.to_table(columns=list(derived_tables.COLUMNS), filter=where).to_pylist()))
        walked = derived_tables.walk(derived_tables.joined(*rows), sources, max_depth=None)
        return cls(sources=frozenset(sources), walked=walked, records=list(records), edges=edges)

    def first_declared(self, keys: set[str]) -> dict[str, datetime | None]:
        """When each of ``keys`` was first declared derived from the source or a table the walk reached: the input record's
        first declaration, or the run time an edge's event names; ``None`` for one an edge names no readable time for."""
        inputs = self.sources | self.walked.depths.keys()
        times: dict[str, list[datetime | None]] = {key: [] for key in keys}
        for entry in self.records:
            if entry.table_uuid in times:
                times[entry.table_uuid] += [declared.first_declared_at for input_uuid, declared in entry.inputs.items() if input_uuid in inputs]
        if self.edges is not None:
            for where in in_lists("out_table", keys):
                for row in self.edges.to_table(columns=["in_table", "out_table", "event_time"], filter=where).to_pylist():
                    if row["in_table"] in inputs:
                        times[str(row["out_table"])].append(_run_time(row["event_time"]))
        return {key: None if None in found else min(at for at in found if at is not None) for key, found in times.items() if found}


def _run_time(event_time: object) -> datetime | None:
    """The time an edge's run event names, or ``None`` when it names none that reads as one."""
    try:
        at = datetime.fromisoformat(str(event_time))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


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
        return SurfaceResult(surface=surface, outcome="followed", detail=f"{name}, derived from this table: {_FOLLOWED}")
    if key in held:
        owed = (
            f"{name}, derived from this table: no erasure obligation follows this one on it: whoever may erase it records one with a "
            f"predicate of its own, naming `follows`: {obligation_id} (undropping it first while it is in the trash)"
        )
        return SurfaceResult(surface=surface, outcome="failed", detail=owed)
    if key in kept:
        return SurfaceResult(surface=surface, outcome="held", detail=f"{name}, derived from this table: {_DEREGISTERED}")
    return SurfaceResult(surface=surface, outcome="gone", detail=f"{name}, derived from this table: {_DROPPED if key in released else _RELOCATED}")
