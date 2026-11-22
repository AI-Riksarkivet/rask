"""A table's rename: its pointer moves under its location claim's fence, and no data byte is read, written or deleted."""

from __future__ import annotations

import logging
from collections.abc import Callable

from lance_namespace import (
    ConcurrentModificationError,
    DeregisterTableRequest,
    InvalidInputError,
    LanceNamespace,
    RegisterTableRequest,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.core.identifiers import rename_destination
from catalog.core.table_objects import holds_a_version, relative_location
from catalog.services import table_claims
from catalog.services.tables.creation import existing_location
from lakehouse.naming import CATALOG_DELIMITER
from lakehouse.records import location_claims
from lakehouse.storage.options import StorageOptions


__all__ = [
    "Reparent",
    "rename_table",
]


log = logging.getLogger(__name__)


class Reparent(BaseModel):
    """How a rename into another namespace takes along where its table inherits from, a step at a time, each called
    from the rename's thread while it holds the location's claim fenced (:func:`rename_table`): ``leave`` before the
    name moves, ``arrive`` once the destination is registered, ``put_back`` for a rename that is undone.
    """

    model_config = ConfigDict(frozen=True)

    #: Move it to the destination, before the name moves. When it raises, it has returned what it may have moved.
    leave: SkipValidation[Callable[[], None]]
    #: Name the table a child of the destination, once its name is registered there.
    arrive: SkipValidation[Callable[[], None]]
    #: Return it to the source, for a rename that is undone.
    put_back: SkipValidation[Callable[[], None]]


def rename_table(
    ns: LanceNamespace,
    source: table_claims.ResolvedTable,
    new_table_name: str,
    *,
    new_namespace_id: list[str] | None,
    so: StorageOptions,
    claims: location_claims.ClaimStore,
    root: str,
    reparent: Reparent | None = None,
) -> tuple[list[str], str]:
    """Rename a table by moving its POINTER. No data byte is read, written or deleted.

    A rename is a namespace-layer remap — Lance has no format-level rename, and the spec's request
    carries identifiers and no location. lance-ns V2 stores a table at ``<hash>_<object_id>`` with the
    mapping in the ``__manifest`` table; the hash is there for object-store throughput and for
    create/delete/recreate conflict prevention, and the spec says the ``object_id`` suffix "ensures
    uniqueness and aids debugging" (`lance_docs/ns_catalog/catalog/dir/index.md`, *Manifest Table Directory*). It is a
    label, not the resolution path. So the whole rename is: take the location's claim for the
    destination, deregister the source, and register the destination id at the source's location.

    That makes it **O(1) in the dataset**, which is the point. The cost of a rename used to be the
    dataset's size, paid inside a request handler that answered 200 — unbounded work no pod sizing
    fixes. Three failure modes go with
    it: the half-copy, the non-atomic source delete that could strand bytes, and the read-rewrite that
    would have collapsed version history.

    MEASURED on the ``dir`` backend the chart runs (``LANCE_REST_IMPL=dir``, pylance 10.0.0,
    2026-09-04): after the two calls, ``describe_table`` on the destination resolves to the source's
    own location, with rows and version history intact and the directory keeping its old object_id
    suffix.

    **A V1 ROOT-namespace table is REFUSED.** Compatibility mode stores those at ``<name>.lance``,
    where the location IS the name, and the spec's own rule is that renaming one "transitions to the
    V2 hash-based path naming" — a relocation. Serving it with a byte copy would put the unbounded
    work straight back; rask's ``require_parent`` guard means no table reachable through these doors
    has that shape, so the refusal costs nothing and names the reason.

    Returns ``(new_segments, location)`` — the location UNCHANGED, because that is the property.
    Raises ``TableNotFoundError`` (source missing / declared-only), ``TableAlreadyExistsError``
    (destination taken) or ``ConcurrentModificationError`` (another rename or registration holds the
    location) so the endpoint maps 404 / 409 / 409.

    THE LOCATION CLAIM IS THE ARBITRATION ([[LH-204]]). ``source`` is the table the request's name resolved to when
    it reached the door, its location and that location's claim as read then (ADR 0173 §7, [[LH-521]]). Before
    either backend call the claim is handed from the source id to the destination id under the ETag that read
    found (:mod:`lakehouse.records.location_claims`), so of N concurrent renames of one source exactly one
    holds the location and the rest answer code 14, and so does a rename of a table a drop or deregister is
    acting on. Nothing in the backend can do this: measured on
    pylance 12.0.0 (lh204 m1), concurrent deregisters of one id all succeed and a register at an occupied
    location commits, so eight barrier-threaded renames left eight live ids on one dataset, 4 of 4 rounds.

    The two backend calls are not one transaction. A crash between them leaves the table reachable by
    no id with its claim naming the destination; a re-register of either id at the location, after the
    claim's lease, recovers it.

    THE CLAIM IS FENCED FROM ITS HAND-OVER UNTIL THE TABLE IS SETTLED ([[LH-586]]). Between the hand-over and the
    destination's registration the claim names a table the backend does not describe, which every other take reads
    as a holder that is gone, and a registration of the destination's own name as an ended table's claim to replace
    under a uuid of its own. The fence refuses them all, and every drop, deregister and rename of the table, until
    this request lifts it once the table is whole under its new name, or hands the claim back under its token. A
    RegisterTable of the destination's name attaches its row before the claim refuses it, so a destination found
    registered at this very location as the rename registers it is that row, never another rename's: the source is
    put back and the name's conflict answered.

    WHERE THE TABLE INHERITS FROM MOVES UNDER THE CLAIM ([[LH-568]]). ``reparent`` is how a rename into another
    namespace takes it along (:class:`Reparent`). It leaves the source, arrives and is put back while this request
    holds the claim, so that move and the name's have the one arbiter and only the request holding the claim makes
    either. It leaves the source BEFORE the name moves, because the two are not one transaction either: a table that
    inherits from the namespace it is about to enter is read by nobody its move would not admit, while one already
    named there and still inheriting from the namespace it left is read by everyone the move should have shut out.
    When leaving raises, the step has returned what it may have moved, and the claim is handed back. Whenever the
    rename is undone the move is put back first, and only then is the claim handed back. It arrives once the
    destination is registered, before the fence is lifted, so nothing else acts on the table until its edges are
    whole. A crash in between leaves the source inheriting from the destination's namespace, which renaming it there
    again finishes.
    """
    segments = source.segments
    if not new_table_name.strip():
        raise InvalidInputError("new_table_name is required")
    new_segments = rename_destination(segments, new_namespace_id, new_table_name)
    if new_segments == segments:
        raise InvalidInputError("the rename destination is identical to the source")
    source_uri = _renamed_location(ns, segments, new_segments, source=source, so=so)
    # RELATIVE, because the dir backend refuses an absolute URI here ("Absolute URIs are not allowed
    # for register_table"): the key the source's location decodes to under the namespace connection's
    # own root, ``root``, as both undrops re-attach a table (:func:`relative_location`, [[LH-528]]).
    # NO BRANCH GUARD, and its absence is a decision. A branch is a shallow clone referencing its
    # source root by ABSOLUTE path, so the byte-copy rename this door used to perform orphaned every
    # branch — it copied the root, deleted the source, and answered 200 over unreadable branch data.
    # The pointer move copies and deletes nothing: measured on the `dir` backend, `branches.list()`
    # after a rename returns the identical entry and the data reads back, because the bytes never
    # moved. Re-adding a refusal would decline a safe operation.
    location = relative_location(source_uri, root=root)
    # THE CLAIM IS HANDED OVER FIRST, and it is the whole of this door's race arbitration: the loser of
    # two renames of one source finds the claim changed since it read it, or reads the winner's claim, and answers
    # code 14 before touching the backend. The source is still retired before the destination is written, so a rename never shows
    # two ids on the dataset even for the instant between the two calls. The token makes this request's
    # claim its own: two renames into the SAME destination name one holder, and only the request that
    # stamped the claim may read it as its retry. The claim is handed over FENCED under that token ([[LH-586]]):
    # until the destination is registered it names a table the backend does not describe, which a registration of
    # the destination's name would otherwise replace under a uuid of its own.
    source_id, dest_id = CATALOG_DELIMITER.join(segments), CATALOG_DELIMITER.join(new_segments)
    fenced = _hand_over_the_claim(ns, claims, source, new_segments=new_segments, source_id=source_id, dest_id=dest_id)
    move = _Move(
        ns=ns,
        claims=claims,
        source_uri=source_uri,
        segments=segments,
        new_segments=new_segments,
        location=location,
        source_id=source_id,
        dest_id=dest_id,
        fenced=fenced,
    )
    put_back = _move_inheritance_to_the_destination(move, reparent)
    try:
        ns.deregister_table(DeregisterTableRequest(id=segments))
    except Exception:
        _hand_back(move, put_back)
        raise
    _register_destination(move, put_back)
    if reparent is not None:
        try:
            reparent.arrive()
        except Exception as exc:
            # The rename has happened, so nothing is raised over it: what the arrival left unwritten is logged.
            log.error("rename_arrival_not_settled", extra={"source": source_id, "destination": dest_id, "error": str(exc)[:300]}, exc_info=True)
    # The table is settled under its new name, so the claim is every door's to act on again.
    table_claims.lift_fence(claims, fenced)
    return new_segments, source_uri


class _Move(BaseModel):
    """A rename past its claim's hand-over: what it moves between and the fence it holds."""

    # `arbitrary_types_allowed` for `ns`: a LanceNamespace is no pydantic type.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # A namespace connection is opaque: what the rename was handed is what it uses, a stand-in in a test included.
    ns: SkipValidation[LanceNamespace]
    claims: location_claims.ClaimStore
    source_uri: str
    segments: list[str]
    new_segments: list[str]
    location: str
    source_id: str
    dest_id: str
    fenced: table_claims.DoorFence


def _renamed_location(ns: LanceNamespace, segments: list[str], new_segments: list[str], *, source: table_claims.ResolvedTable, so: StorageOptions) -> str:
    """The location of the written table ``source`` a rename moves from ``segments`` to the free name ``new_segments``."""
    source_uri = source.location
    if source_uri is None or not holds_a_version(source_uri, so):
        # Missing OR declared-but-unwritten → nothing to repoint (404, symmetric with every op that
        # requires a written table).
        raise TableNotFoundError(f"table not found: {'.'.join(segments)}")
    if len(segments) == 1:
        raise InvalidInputError(
            f"cannot rename {'.'.join(segments)}: it is a root-namespace table, which the directory catalog stores "
            "under V1 compatibility naming (<name>.lance) where the location IS the name. The spec's rule is that such "
            "a rename transitions the table to V2 hash-based naming, which relocates its data — unbounded work this "
            "door will not do in a request. Move the table into a namespace first."
        )
    # The destination must be free in EVERY form, checked BEFORE anything is touched. A declared-only
    # stub is TAKEN, never adopted: adopting one skips `register_table`, which is what arbitrates two
    # concurrent renames into the SAME destination name — the source-side race is arbitrated by the
    # location claim below, and the two together are what make this door safe under concurrency.
    if existing_location(ns, new_segments) is not None:
        raise TableAlreadyExistsError(f"table already exists: {'.'.join(new_segments)}")
    return source_uri


def _hand_over_the_claim(
    ns: LanceNamespace, claims: location_claims.ClaimStore, source: table_claims.ResolvedTable, *, new_segments: list[str], source_id: str, dest_id: str
) -> table_claims.DoorFence:
    """Hand the source's location claim over to ``dest_id``, fenced under this request's token; a held claim is a 409."""
    try:
        return table_claims.hand_over(ns, claims, source, table=dest_id, segments=new_segments, previous=source_id)
    except (location_claims.LocationHeldError, location_claims.ClaimChangedError) as refused:
        holder = refused.claim.table if isinstance(refused, location_claims.LocationHeldError) else None
        log.warning("rename_refused_held_location", extra={"source": source_id, "location": source.location, "holder": holder})
        raise ConcurrentModificationError(
            f"cannot rename {source_id}: its location is held by another table, by a concurrent rename or registration; describe it and retry"
        ) from None


def _move_inheritance_to_the_destination(move: _Move, reparent: Reparent | None) -> Callable[[], None] | None:
    """Move where the table inherits from to the destination: how to put it back, ``None`` when nothing moves along."""
    if reparent is None:
        return None
    try:
        reparent.leave()
    except Exception:
        # A leave that raises has returned what it may have moved, and the claim is handed back.
        _hand_back(move, None)
        raise
    return reparent.put_back


def _hand_back(move: _Move, put_back: Callable[[], None] | None) -> None:
    """Hand the claim of a rename that is undone back to the source, after ``put_back`` returns what followed the table."""
    # The source keeps its location, so it keeps the claim; left with the destination, every later
    # rename of the source would answer code 14 for the claim's lease. What followed the table goes back
    # first, while the claim still keeps every other request off it. Under this request's token, since the
    # claim is fenced for it, and the claim the source takes back carries no fence. Logged, never raised over
    # the failure that made the rename compensate.
    if put_back is not None:
        try:
            put_back()
        except Exception as exc:
            log.error("rename_parent_not_put_back", extra={"source": move.source_id, "destination": move.dest_id, "error": str(exc)[:300]}, exc_info=True)
    try:
        table_claims.take(move.ns, move.claims, move.source_uri, table=move.source_id, segments=move.segments, previous=move.dest_id, token=move.fenced.token)
    except Exception as exc:
        log.error("rename_claim_not_handed_back", extra={"source": move.source_id, "location": move.source_uri, "error": str(exc)[:300]}, exc_info=True)


def _register_destination(move: _Move, put_back: Callable[[], None] | None) -> None:
    """Register the deregistered table under its new name; one that fails registers the source again and hands back."""
    try:
        move.ns.register_table(RegisterTableRequest(id=move.new_segments, location=move.location))
    except Exception:
        # COMPENSATE, or a rename that trips on its destination leaves the table reachable by NO id —
        # bytes intact and invisible. The same call at the same location the source already had, and
        # the claim handed back with it. A destination found registered at this very location is no finished
        # rename, since no other rename holds the claim this one fenced: it is the row a RegisterTable of the
        # destination's name attaches before the claim refuses it, which that registration then detaches itself.
        try:
            move.ns.register_table(RegisterTableRequest(id=move.segments, location=move.location))
        except Exception as restore:
            # The source is now unreachable and the location is the only way back, so it goes in the
            # log rather than being swallowed with the original error.
            log.error(
                "rename_source_pointer_lost",
                extra={"source": move.source_id, "location": move.location, "error": str(restore)},
                exc_info=restore,
            )
        else:
            _hand_back(move, put_back)
        raise
