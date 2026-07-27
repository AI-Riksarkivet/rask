"""Response models for the knowledge-graph endpoints (``/api/graph``).

The graph is four Lance tables produced offline by the LightRAG build +
adapter: ``kg_entities`` (deduped entities), ``kg_chunks`` (the 30 s transcript
clips entities were extracted from), ``kg_mentions`` (entity→chunk), and
``kg_relationships`` (entity→entity edges LightRAG pulled from the text). Every
endpoint returns ``built: false`` when ``kg_entities.lance`` is absent.
"""

from pydantic import BaseModel, Field


class GraphStatusResponse(BaseModel):
    """Row counts for the explorer's empty-state / header, or ``built: false``."""

    built: bool
    entities: int = 0
    relations: int = 0
    mentions: int = 0
    videos: int = 0


class CypherResponse(BaseModel):
    """A free-form Cypher result as columns + rows (shell-style).

    Invalid Cypher is NOT an HTTP error — ``error`` carries the engine message
    and ``columns``/``rows`` stay empty, so the UI renders it inline like a REPL.
    """

    built: bool
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str | float | int | None]] = Field(default_factory=list)
    error: str | None = None


class EntityMatch(BaseModel):
    """One entity-search hit (ranked by ``mention_count``)."""

    entity_id: str
    name: str
    entity_type: str
    mention_count: int
    videos: int


class GraphSearchResponse(BaseModel):
    """Top entity-name matches for the search box, or ``built: false``."""

    built: bool
    matches: list[EntityMatch] = Field(default_factory=list)


class EntityClip(BaseModel):
    """A playable 30 s clip an entity was mentioned in (entity→MENTIONS→Chunk)."""

    chunk_id: str
    doc_id: str
    namn: str
    start: float
    end: float
    text: str


class EntityNeighbor(BaseModel):
    """A RELATIONSHIP-edge neighbour of an entity. ``direction`` is out|in."""

    entity_id: str
    name: str
    entity_type: str
    direction: str
    description: str


class EntityCooccur(BaseModel):
    """Another entity that shares a clip with this one; ``shared`` = clip count."""

    entity_id: str
    name: str
    shared: int


class EntityDetail(BaseModel):
    """An entity's own properties (the side-panel header)."""

    entity_id: str
    name: str
    entity_type: str
    mention_count: int


class GraphEntityResponse(BaseModel):
    """One entity's properties + clips + relationship neighbours + co-occurrences.

    ``entity`` is ``None`` (with empty lists) when the id is unknown or the graph
    is not built.
    """

    built: bool
    entity: EntityDetail | None = None
    clips: list[EntityClip] = Field(default_factory=list)
    neighbors: list[EntityNeighbor] = Field(default_factory=list)
    cooccur: list[EntityCooccur] = Field(default_factory=list)


class GraphNode(BaseModel):
    """A node for the force-layout view."""

    id: str
    name: str
    type: str
    mentions: int
    videos: int


class GraphEdge(BaseModel):
    """A RELATIONSHIP edge for the force-layout view."""

    source: str
    target: str
    description: str


class SubgraphResponse(BaseModel):
    """Nodes + edges for the WebGPU graph view, or ``built: false``.

    With no ``entity_id`` this is the overview (top entities by mention count);
    with one it is that entity plus its 1-hop relationship neighbourhood.
    """

    built: bool
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
