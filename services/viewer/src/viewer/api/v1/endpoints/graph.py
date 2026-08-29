"""Knowledge-graph endpoints — lance-graph Cypher over the ``kg_*`` Lance tables.

Port of the old ``backend/graph/router.py``: the entities table now comes from
the descriptor's ``graph`` capability (its value IS the entities table name);
the three sibling tables are derived by replacing its ``entities`` suffix with
``chunks`` / ``mentions`` / ``relationships`` (the KG adapter's naming
contract). A single :class:`lance_graph.CypherEngine` is built over pyarrow
snapshots of the four tables and cached PER APP (see :class:`_GraphCache`), keyed by
``(db path, entities-table version)`` — so a KG rebuild is picked up without a
server restart (the cache key changes), while repeat requests reuse the
engine and a cold build for one dataset never blocks a read of another. Every endpoint returns ``built: false`` when the capability is
undeclared or any table is absent.

``/cypher`` runs arbitrary read Cypher (the explorer's REPL); invalid queries
come back as a 200 with ``error`` set, not an HTTP failure. The ``q`` /
``entity_id`` inputs to the other routes are whitelisted before they are ever
inlined into a Cypher literal — entity ids are 16-char hex slugs.
"""

from __future__ import annotations

import logging
import re
import threading
import weakref
from collections import OrderedDict
from typing import TYPE_CHECKING, Annotated, Any

import lance
import lance_graph as lg
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict

from service_kit.lancekit import store
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import AppState, dataset_handle
from viewer.api.security import REQUIRE_CORPUS_DATA, REQUIRE_CORPUS_METADATA
from viewer.schemas.graph import (
    CypherResponse,
    EntityClip,
    EntityCooccur,
    EntityDetail,
    EntityMatch,
    EntityNeighbor,
    GraphEdge,
    GraphEntityResponse,
    GraphNode,
    GraphSearchResponse,
    GraphStatusResponse,
    SubgraphResponse,
)


if TYPE_CHECKING:
    from collections.abc import Callable

    from service_kit.lancekit.descriptor import Declared
    from service_kit.lancekit.registry import DatasetHandle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/graph", tags=["graph"])

#: The graph capability's entities table always ends with this suffix; the
#: sibling tables replace it (``kg_entities`` → ``kg_chunks`` / ``kg_mentions``
#: / ``kg_relationships``) — the KG adapter's naming contract.
_ENTITIES_SUFFIX = "entities"

# The kg_* tables' column names are the graph capability's writer-guaranteed
# contract (the LightRAG adapter always produces exactly these), NOT corpus
# schema — so they stay module constants instead of descriptor lookups.
_ENTITY_ID_COL = "entity_id"
_ENTITY_NAME_COL = "name"
_ENTITY_TYPE_COL = "entity_type"
_ENTITY_MENTIONS_COL = "mention_count"
_CHUNK_ID_COL = "chunk_id"
_CHUNK_DOC_COL = "doc_id"
_MENTION_SOURCE_COL = "source_entity_id"
_MENTION_TARGET_COL = "target_chunk_id"
_REL_SOURCE_COL = "source_entity_id"
_REL_TARGET_COL = "target_entity_id"
_REL_DESCRIPTION_COL = "description"

#: entity_id is sha1(name)[:16] — 16 lowercase hex chars. Validated before any
#: Cypher interpolation so a crafted id can't break out of the single-quoted
#: literal (the same threat the doc_id whitelist guards elsewhere).
_ENTITY_ID = re.compile(r"^[0-9a-f]{16}$")

#: A Cypher-safe column identifier — descriptor-fed preset columns are matched
#: against this before they are inlined into a query.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000

# Overview ranking preference — specific named entities (people/places/events)
# rank above orgs/works, which rank above abstract CONCEPT and OTHER. Within a
# tier, mention_count breaks the tie. Corpus content pending descriptor-ization
# (these are the LightRAG extraction's type labels, not schema names).
_OVERVIEW_TYPE_RANK: dict[str, int] = {
    "PERSON": 0,
    "GEO": 0,
    "EVENT": 0,
    "ORG": 1,
    "WORK": 2,
    "CONCEPT": 3,
    "OTHER": 4,
}

#: Trailing ``LIMIT <n>`` (the only place a LIMIT can legally sit in a single
#: read statement). Matched case-insensitively at end-of-query.
_TRAILING_LIMIT = re.compile(r"(?is)\blimit\s+(\d+)\s*$")

# EntityClip's one non-structural field is the corpus display column the clip
# query surfaces. Its name is read from the response model at import time so
# it lives only in service_kit.schemas (this module stays corpus-literal-free);
# the matching kg-chunks column comes from the descriptor's ``graph_presets``.
_CLIP_STRUCTURAL_FIELDS = frozenset({"chunk_id", "doc_id", "start", "end", "text"})
_CLIP_TITLE_FIELD = next(f for f in EntityClip.model_fields if f not in _CLIP_STRUCTURAL_FIELDS)


class GraphPresets(BaseModel):
    """Optional ``graph_presets`` extra field on the declared descriptor.

    Corpus content pending full descriptor-ization: today it carries only the
    kg-chunks column the clip cards show as their title (falls back to the doc
    key when unset).
    """

    clip_title_column: str | None = None


def _enforce_limit(query: str, cap: int) -> str:
    """Guarantee the query returns at most ``cap`` rows.

    Clamps a user-supplied trailing ``LIMIT n`` down to ``cap`` (the old code
    only *appended* a cap when the word ``limit`` was textually absent, so
    ``LIMIT 999999`` — or any query merely containing the substring "limit",
    e.g. in a string literal — escaped the cap entirely and could pull the
    whole table). Appends ``LIMIT cap`` when no trailing LIMIT is present.
    """
    trailing = _TRAILING_LIMIT.search(query)
    if trailing is None:
        return f"{query} LIMIT {cap}"
    n = min(int(trailing.group(1)), cap)
    return _TRAILING_LIMIT.sub(f"LIMIT {n}", query)


class _KgTables(BaseModel):
    """The four kg table names derived from the graph capability."""

    entities: str
    chunks: str
    mentions: str
    relationships: str


def _kg_tables(declared: Declared) -> _KgTables | None:
    target = declared.capabilities.get("graph")
    if target is None:
        return None
    entities = target.partition(".")[0]
    if not entities.endswith(_ENTITIES_SUFFIX):
        logger.warning("graph capability %r does not end with %r", entities, _ENTITIES_SUFFIX)
        return None
    prefix = entities[: -len(_ENTITIES_SUFFIX)]
    return _KgTables(
        entities=entities,
        chunks=f"{prefix}chunks",
        mentions=f"{prefix}mentions",
        relationships=f"{prefix}relationships",
    )


class _GraphResources(BaseModel):
    """Cached, request-shared graph handles + precomputed lookups.

    Holds the live :class:`CypherEngine` (which keeps its four pyarrow tables
    alive) plus small dicts/lists derived once so the non-Cypher routes
    (status, search, subgraph) are pure in-memory ops.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    engine: Any  # lance_graph.CypherEngine
    ent_by_id: dict[str, dict[str, Any]]
    ent_videos: dict[str, int]
    rels: list[tuple[str, str, str]]
    n_mentions: int
    n_videos: int
    clip_title_column: str | None


#: Bound the KG engine cache — each built engine is ~hundreds of MB, so a
#: superseded version must not linger. Small: KGs rebuild rarely.
_CACHE_MAX = 2

#: Cache key: (db path, entities-table version). The version bump on a rebuild changes the key, so
#: a stale engine is never served.
type _CacheKey = tuple[str, int]


class _GraphCache:
    """One app's KG engines, bounded, with SINGLE-FLIGHT PER KEY (VS-08).

    Two things were wrong with the module-global dict this replaces.

    *It was shared by every app in the process.* ``create_viewer_app`` can build several apps over
    different ``AppState``s — the composition seam the codebase deliberately has — and they all
    contended for one ``_CACHE_MAX`` budget while nothing released the ~370 MB engines when an app
    was torn down. A cache is per-app state, so it lives beside the app, not beside the module.

    *Its lock wrapped the BUILD.* One global lock around a ~20 s ``_build_resources`` meant that a
    cold build for dataset A blocked every ``/api/graph/*`` request for dataset B — including the
    cheap ``GET /status`` — for that whole window, each waiter holding a threadpool thread. With 40
    workers, a handful of cold graph requests starve every sync route in the process.

    So: a short guard around the dict operations ONLY, plus one lock per cache key. Concurrent first
    requests for the SAME key still collapse into one build (the thundering-herd guarantee the old
    lock did provide, and the reason a plain ``setdefault`` is not enough); a build for another key
    runs in parallel.
    """

    def __init__(self, max_entries: int = _CACHE_MAX) -> None:
        self._entries: OrderedDict[_CacheKey, _GraphResources] = OrderedDict()
        self._locks: dict[_CacheKey, threading.Lock] = {}
        # Held only for dict reads/writes — NEVER across a build. That is the whole fix.
        self._guard = threading.Lock()
        self._max = max_entries

    def get_or_build(self, key: _CacheKey, build: Callable[[], _GraphResources]) -> _GraphResources:
        with self._guard:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                # The winner of the race built it while we waited on ITS key's lock.
                cached = self._entries.get(key)
                if cached is not None:
                    return cached
            try:
                built = build()
            except BaseException:
                # Drop the key lock on failure too, so a transient error does not leave `_locks`
                # holding an entry for a key that has no engine.
                with self._guard:
                    self._locks.pop(key, None)
                raise
            with self._guard:
                # Publish and retire the key lock UNDER ONE GUARD. Retiring it first would open a
                # window in which a newly arriving thread finds neither the entry nor the lock,
                # mints a second lock, and runs a second ~20 s build.
                self._entries[key] = built
                while len(self._entries) > self._max:
                    self._entries.popitem(last=False)
                self._locks.pop(key, None)
            return built


#: Per-app caches, keyed by ``id(state)`` with a finalizer that drops the entry when the app's
#: ``AppState`` is collected — which is what makes an engine die with the app that built it.
#: ``id()`` rather than a ``WeakKeyDictionary`` because ``AppState`` is a Pydantic model and
#: therefore unhashable; the finalizer gives the same lifetime guarantee. It is deliberately NOT a
#: field on ``AppState``: that model lives in ``service_kit`` and is shared with the search service
#: and the annotator, and the knowledge graph is a viewer-only concern.
_CACHES: dict[int, _GraphCache] = {}
_CACHES_GUARD = threading.Lock()


def _cache_for(state: AppState) -> _GraphCache:
    """This app's engine cache, created on first use and released with the app."""
    with _CACHES_GUARD:
        cache = _CACHES.get(id(state))
        if cache is None:
            cache = _GraphCache()
            _CACHES[id(state)] = cache
            weakref.finalize(state, _drop_cache, id(state))
        return cache


def _drop_cache(state_id: int) -> None:
    with _CACHES_GUARD:
        _CACHES.pop(state_id, None)


def _clip_title_column(declared: Declared) -> str | None:
    raw = (declared.model_extra or {}).get("graph_presets")
    if raw is None:
        return None
    column = GraphPresets.model_validate(raw).clip_title_column
    if column is not None and _IDENTIFIER.fullmatch(column) is None:
        # Config, not user input — but it ends up inlined in Cypher, so a
        # malformed value is dropped rather than interpolated.
        logger.warning("ignoring non-identifier graph_presets.clip_title_column %r", column)
        return None
    return column


def _build_resources(handle: DatasetHandle, names: _KgTables) -> _GraphResources:
    files = {
        "Entity": names.entities,
        "Chunk": names.chunks,
        "MENTIONS": names.mentions,
        "RELATIONSHIP": names.relationships,
    }
    tables = {label: lance.dataset(handle.table_uri(name), storage_options=handle.storage_options).to_table() for label, name in files.items()}
    cfg = (
        lg.GraphConfigBuilder()
        .with_node_label("Entity", _ENTITY_ID_COL)
        .with_node_label("Chunk", _CHUNK_ID_COL)
        .with_relationship("MENTIONS", _MENTION_SOURCE_COL, _MENTION_TARGET_COL)
        .with_relationship("RELATIONSHIP", _REL_SOURCE_COL, _REL_TARGET_COL)
        .build()
    )
    engine = lg.CypherEngine(cfg, tables)

    ent_by_id = {
        row[_ENTITY_ID_COL]: {
            "name": row[_ENTITY_NAME_COL],
            "entity_type": row[_ENTITY_TYPE_COL],
            "mention_count": int(row[_ENTITY_MENTIONS_COL]),
        }
        for row in tables["Entity"].to_pylist()
    }
    chunk_doc = {r[_CHUNK_ID_COL]: r[_CHUNK_DOC_COL] for r in tables["Chunk"].to_pylist()}
    ent_videos: dict[str, set[str]] = {}
    for m in tables["MENTIONS"].to_pylist():
        doc = chunk_doc.get(m[_MENTION_TARGET_COL])
        if doc is not None:
            ent_videos.setdefault(m[_MENTION_SOURCE_COL], set()).add(doc)
    rels = [(r[_REL_SOURCE_COL], r[_REL_TARGET_COL], r.get(_REL_DESCRIPTION_COL) or "") for r in tables["RELATIONSHIP"].to_pylist()]
    return _GraphResources(
        engine=engine,
        ent_by_id=ent_by_id,
        ent_videos={k: len(v) for k, v in ent_videos.items()},
        rels=rels,
        n_mentions=tables["MENTIONS"].num_rows,
        n_videos=len(set(chunk_doc.values())),
        clip_title_column=_clip_title_column(handle.descriptor.declared),
    )


def _resources(state: AppState, handle: DatasetHandle) -> _GraphResources | None:
    """This APP's cached graph engine + lookups, or ``None`` if the graph isn't built.

    Existence is probed on disk per request (not via the descriptor's startup
    table snapshot) so a KG built after the app started is served without a
    restart.

    ``state`` is what the cache hangs from: the engines are one app's resources, released when that
    app is (see :class:`_GraphCache`), and a build for one dataset never blocks a read of another.
    """
    names = _kg_tables(handle.descriptor.declared)
    if names is None:
        return None
    all_names = (names.entities, names.chunks, names.mentions, names.relationships)
    if not all(store.exists(handle.table_uri(n), handle.storage_options) for n in all_names):
        return None
    version = lance.dataset(handle.table_uri(names.entities), storage_options=handle.storage_options).version
    # Single-flight + bounded: the ~20s/~370MB build must not run N times under a
    # cold-start thundering herd, and superseded versions must not accumulate.
    return _cache_for(state).get_or_build((handle.uri, version), lambda: _build_resources(handle, names))


def _node(res: _GraphResources, entity_id: str) -> GraphNode | None:
    props = res.ent_by_id.get(entity_id)
    if props is None:
        return None
    return GraphNode(
        id=entity_id,
        name=props["name"],
        type=props["entity_type"],
        mentions=props["mention_count"],
        videos=res.ent_videos.get(entity_id, 0),
    )


def _cell(value: Any) -> str | float | int | None:
    return value if value is None or isinstance(value, str | int | float) else str(value)


def _run_rows(res: _GraphResources, cypher: str) -> list[dict[str, Any]]:
    """Execute read Cypher and return rows as dicts (raises on a bad query)."""
    result = res.engine.execute(cypher)
    return result.to_pylist() if hasattr(result, "to_pylist") else list(result)


@router.get("/status", dependencies=[REQUIRE_CORPUS_METADATA])
def get_status(state: StateDep, dataset: DatasetParam = None) -> GraphStatusResponse:
    """Row counts for the explorer header, or ``built: false`` if the KG is absent."""
    res = _resources(state, dataset_handle(state, dataset))
    if res is None:
        return GraphStatusResponse(built=False)
    return GraphStatusResponse(
        built=True,
        entities=len(res.ent_by_id),
        relations=len(res.rels),
        mentions=res.n_mentions,
        videos=res.n_videos,
    )


class CypherRequest(BaseModel):
    """Body for the Cypher REPL endpoint."""

    query: str
    limit: int = _DEFAULT_LIMIT


@router.post("/cypher", dependencies=[REQUIRE_CORPUS_DATA])
def run_cypher(body: CypherRequest, state: StateDep, dataset: DatasetParam = None) -> CypherResponse:
    """Run arbitrary read Cypher; invalid queries return ``error`` (HTTP 200)."""
    res = _resources(state, dataset_handle(state, dataset))
    if res is None:
        return CypherResponse(built=False)
    raw = body.query.strip()
    if not raw:
        return CypherResponse(built=True, error="Empty query.")
    statements = [s for s in raw.split(";") if s.strip()]
    if len(statements) > 1:
        return CypherResponse(built=True, error="Only a single statement is allowed.")
    query = statements[0].strip()
    query = _enforce_limit(query, max(1, min(body.limit, _MAX_LIMIT)))
    try:
        rows = _run_rows(res, query)
    except Exception as exc:
        return CypherResponse(built=True, error=f"{type(exc).__name__}: {exc}")
    columns = list(rows[0].keys()) if rows else []
    return CypherResponse(
        built=True,
        columns=columns,
        rows=[[_cell(row.get(c)) for c in columns] for row in rows],
    )


@router.get("/search", dependencies=[REQUIRE_CORPUS_DATA])
def search_entities(q: str, state: StateDep, dataset: DatasetParam = None) -> GraphSearchResponse:
    """Entity-name substring matches (top 10 by mention count). ``q`` never hits Cypher."""
    res = _resources(state, dataset_handle(state, dataset))
    if res is None:
        return GraphSearchResponse(built=False)
    needle = q.strip().lower()
    if len(needle) < 2:
        return GraphSearchResponse(built=True)
    hits = [
        EntityMatch(
            entity_id=eid,
            name=props["name"],
            entity_type=props["entity_type"],
            mention_count=props["mention_count"],
            videos=res.ent_videos.get(eid, 0),
        )
        for eid, props in res.ent_by_id.items()
        if needle in props["name"].lower()
    ]
    hits.sort(key=lambda m: m.mention_count, reverse=True)
    return GraphSearchResponse(built=True, matches=hits[:10])


def _clip(row: dict[str, Any], title_column: str | None) -> EntityClip:
    # Cypher preset strings return kg-chunk columns as ``c.<column>``. The
    # title falls back to the doc id when the preset column is unset/null.
    title = None
    if title_column is not None:
        title = row.get(f"c.{title_column}")
    return EntityClip.model_validate(
        {
            "chunk_id": str(row["c.chunk_id"]),
            "doc_id": str(row["c.doc_id"]),
            _CLIP_TITLE_FIELD: str(title or row["c.doc_id"]),
            "start": float(row["c.start_s"]),
            "end": float(row["c.end_s"]),
            "text": str(row.get("c.text") or ""),
        }
    )


@router.get("/entity/{entity_id}", dependencies=[REQUIRE_CORPUS_DATA])
def get_entity(entity_id: str, state: StateDep, dataset: DatasetParam = None) -> GraphEntityResponse:
    """An entity's properties + clips + relationship neighbours + co-occurrences.

    Clips, neighbours and co-occurrences are answered through the live
    :class:`CypherEngine` (the prod Cypher path); ``entity: None`` for an
    unknown or malformed id. The Cypher preset strings below are corpus
    content pending descriptor-ization (they name the KG writer contract's
    columns, no corpus schema).
    """
    res = _resources(state, dataset_handle(state, dataset))
    if res is None:
        return GraphEntityResponse(built=False)
    if not _ENTITY_ID.match(entity_id):
        return GraphEntityResponse(built=True)
    props = res.ent_by_id.get(entity_id)
    if props is None:
        return GraphEntityResponse(built=True)

    title_part = f", c.{res.clip_title_column}" if res.clip_title_column else ""
    clip_rows = _run_rows(
        res,
        f"MATCH (e:Entity {{entity_id:'{entity_id}'}})-[:MENTIONS]->(c:Chunk) RETURN c.chunk_id, c.doc_id, c.start_s, c.end_s, c.text{title_part} LIMIT 40",
    )
    clips = sorted(
        (_clip(r, res.clip_title_column) for r in clip_rows),
        key=lambda c: (c.doc_id, c.start),
    )

    neighbors: list[EntityNeighbor] = []
    for direction, pattern in (
        ("out", f"(a:Entity {{entity_id:'{entity_id}'}})-[r:RELATIONSHIP]->(b:Entity)"),
        ("in", f"(b:Entity)-[r:RELATIONSHIP]->(a:Entity {{entity_id:'{entity_id}'}})"),
    ):
        for r in _run_rows(
            res,
            f"MATCH {pattern} RETURN b.entity_id, b.name, b.entity_type, r.description LIMIT 30",
        ):
            neighbors.append(
                EntityNeighbor(
                    entity_id=str(r["b.entity_id"]),
                    name=str(r["b.name"]),
                    entity_type=str(r.get("b.entity_type") or "OTHER"),
                    direction=direction,
                    description=str(r.get("r.description") or ""),
                )
            )

    cooccur_rows = _run_rows(
        res,
        f"MATCH (a:Entity {{entity_id:'{entity_id}'}})-[:MENTIONS]->(c:Chunk)"
        "<-[:MENTIONS]-(b:Entity) "
        f"WHERE b.entity_id <> '{entity_id}' "
        "RETURN b.entity_id, b.name, count(c.chunk_id) AS shared "
        "ORDER BY shared DESC LIMIT 12",
    )
    cooccur = [EntityCooccur(entity_id=str(r["b.entity_id"]), name=str(r["b.name"]), shared=int(r["shared"])) for r in cooccur_rows]

    return GraphEntityResponse(
        built=True,
        entity=EntityDetail(entity_id=entity_id, **props),
        clips=clips,
        neighbors=neighbors,
        cooccur=cooccur,
    )


@router.get("/subgraph", dependencies=[REQUIRE_CORPUS_DATA])
def get_subgraph(
    state: StateDep,
    entity_id: str | None = None,
    # DECLARED, not clamped: `max(1, min(limit, _MAX_LIMIT))` below is the real bound and the
    # schema never said so.
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = 150,
    dataset: DatasetParam = None,
) -> SubgraphResponse:
    """Nodes + edges for the force-layout view.

    No ``entity_id`` → overview (top ``limit`` entities by mention count + every
    relationship among them). With one → that entity plus its 1-hop relationship
    neighbourhood.
    """
    res = _resources(state, dataset_handle(state, dataset))
    if res is None:
        return SubgraphResponse(built=False)
    limit = max(1, min(limit, _MAX_LIMIT))

    if entity_id is not None:
        if not _ENTITY_ID.match(entity_id) or entity_id not in res.ent_by_id:
            return SubgraphResponse(built=True)
        node_ids = {entity_id}
        for src, tgt, _desc in res.rels:
            if src == entity_id:
                node_ids.add(tgt)
            elif tgt == entity_id:
                node_ids.add(src)
        if len(node_ids) > limit:
            ranked = sorted(
                node_ids,
                key=lambda e: (e != entity_id, -res.ent_by_id.get(e, {}).get("mention_count", 0)),
            )
            node_ids = set(ranked[:limit])
    else:
        # Default overview: surface NAMED entities first, not the generic
        # abstractions that dominate by raw mention count. Rank by a coarse
        # type preference, then mention_count within each tier — so the first
        # thing a user sees is people/places/events/orgs, not CONCEPT/OTHER.
        ranked = sorted(
            res.ent_by_id,
            key=lambda e: (
                _OVERVIEW_TYPE_RANK.get(res.ent_by_id[e].get("entity_type", "OTHER"), 9),
                -res.ent_by_id[e]["mention_count"],
            ),
        )
        node_ids = set(ranked[:limit])

    nodes = [n for eid in node_ids if (n := _node(res, eid)) is not None]
    # dedupe undirected: collapse both per-mention repeats AND a reversed pair
    # so an edge never renders twice in the force layout.
    seen: set[tuple[str, str]] = set()
    edges: list[GraphEdge] = []
    for src, tgt, desc in res.rels:
        key = (src, tgt) if src <= tgt else (tgt, src)
        if src in node_ids and tgt in node_ids and key not in seen:
            seen.add(key)
            edges.append(GraphEdge(source=src, target=tgt, description=desc))
    return SubgraphResponse(built=True, nodes=nodes, edges=edges)
