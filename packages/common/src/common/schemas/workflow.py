"""Server-side mirror of the media zone's persisted workflow graph + saved views.

These are the shapes a user's own work takes when it leaves the browser. They are a MIRROR, not a new
contract: field-for-field they match the valibot schemas the zone already parses its localStorage snapshot
with —``frontend/components/frontends/media/src/lib/workflow/persistence.ts`` (the graph) and the
``SavedView`` interface in ``frontend/components/frontends/media/src/lib/saved-views.ts``. Wire names stay
camelCase via ``to_camel`` so a document written by the browser round-trips byte-compatibly; the Python
attribute names stay snake_case.

Two deliberate divergences from valibot, both because a server is a BOUNDARY and localStorage is a CACHE:

* **No self-healing.** valibot uses ``v.fallback`` so a stale/corrupt snapshot degrades to defaults rather
  than crashing the canvas. Here a malformed document is a 422 — we validate at the boundary and say what
  is wrong, instead of silently storing something the client did not mean (writing-python
  ``references/error-handling.md``: validate early, fail fast with context). ``n`` is the visible case:
  valibot CLAMPS to [1, 100], we reject outside it.
* **Unknown keys are dropped, not rejected** (pydantic's default ``extra='ignore'``) — that half DOES match
  valibot's ``v.object``, which strips. A zone shipping a new field ahead of the catalog loses the field,
  not the document.

The one thing callers must preserve is null-omission on the way out: the TS edge schema is
``v.optional(v.string())`` (absent is fine, ``null`` is a parse FAILURE), and ``SearchSpec`` is written
under ``exactOptionalPropertyTypes`` as ``T | undefined``. So every route serving these models must set
``response_model_exclude_none=True`` — see ``catalog/api/v1/endpoints/user_state.py``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class WorkflowNodeKind(StrEnum):
    """The pipeline stages — mirrors ``NODE_KINDS`` in the zone's ``workflow/types.ts``."""

    QUERY = "query"
    IMAGE = "image"
    FILTER = "filter"
    ATLAS = "atlas"
    SEARCH = "search"
    COMBINE = "combine"
    TAGGER = "tagger"
    RESULTS = "results"
    EXPORT = "export"


class WorkflowSearchMode(StrEnum):
    """Mirrors the zone's ``MODE_VALUES`` (itself ``satisfies readonly SearchMode[]``).

    Duplicated rather than imported from ``search.services.spec``: ``common`` is the shared kernel every
    service imports, so importing a service package here would invert the layering. The duplication cannot
    drift — ``tests/unit/test_user_state.py`` asserts these members equal ``SearchMode``'s.
    """

    FTS = "fts"
    SEMANTIC = "semantic"
    VISUAL = "visual"
    SCENE = "scene"
    SCENE_FTS = "scene_fts"
    HYBRID = "hybrid"
    ALL = "all"


#: Result-count bounds, mirroring ``DEFAULT_N`` / ``MIN_N`` / ``MAX_N`` in ``workflow/types.ts``.
DEFAULT_N = 24
MIN_N = 1
MAX_N = 100


class _CamelModel(BaseModel):
    """Base for every mirrored shape: camelCase on the wire, snake_case in Python, unknown keys dropped."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class WorkflowNodePosition(_CamelModel):
    """A node's canvas coordinates."""

    x: float
    y: float


class WorkflowNode(_CamelModel):
    """A persisted node: identity, kind and position. Runtime results are never persisted."""

    id: str
    type: WorkflowNodeKind
    position: WorkflowNodePosition


class WorkflowEdge(_CamelModel):
    """A persisted edge. ``target_handle`` distinguishes the Search node's two input ports."""

    id: str
    source: str
    target: str
    target_handle: str | None = None
    label: str | None = None


class WorkflowNodeConfig(_CamelModel):
    """Per-node user input — one flat shape covering every kind, as the zone stores it.

    ``captured_atlas_selection`` is typed ``None`` on purpose: the zone deliberately does NOT persist the
    atlas capture (a full ``Row[]`` — too heavy and stale-prone), so a document carrying one is a client
    bug, not something to store.
    """

    q: str = ""
    image_name: str = ""
    where: str = ""
    filters: dict[str, str] = Field(default_factory=dict)
    mode: WorkflowSearchMode = WorkflowSearchMode.FTS
    n: int = Field(default=DEFAULT_N, ge=MIN_N, le=MAX_N)
    rerank: bool = False
    min_score: float | None = None
    refine_scope: Literal["video", "chunk"] = "video"
    combine_mode: Literal["union", "intersect"] = "union"
    tags: list[str] = Field(default_factory=list)
    export_format: Literal["json", "csv"] = "csv"
    export_columns: list[str] | None = None
    captured_atlas_selection: None = None
    label: str = ""
    enabled: bool = True


class WorkflowGraph(_CamelModel):
    """A whole persisted canvas — the document behind ``lance-media-workflow-graph-v1``.

    ``nodes`` must be non-empty, mirroring ``v.minLength(1)``: an empty canvas is what the client seeds,
    not something worth storing.
    """

    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(default_factory=list)
    config: dict[str, WorkflowNodeConfig] = Field(default_factory=dict)
    tags: dict[str, list[str]] = Field(default_factory=dict)


class SearchSpec(_CamelModel):
    """A read-plane selection — mirrors the ``SearchSpec`` interface in ``@repo/media-api``.

    ``image`` (a ``File``) is absent by design: ``stripEphemeral`` drops it before a view is saved, and a
    file handle cannot round-trip anyway.
    """

    q: str
    n: int | None = None
    mode: WorkflowSearchMode | None = None
    rerank: bool | None = None
    rerank_n: int | None = None
    fuzziness: Literal[0, 1, 2] | None = None
    phrase: bool | None = None
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    q_vec: str | None = None
    where: str | None = None
    prefilter: bool | None = None
    filters: dict[str, str] | None = None
    topic: str | None = None
    dataset: str | None = None


class SavedView(_CamelModel):
    """A named selection for a dataset (``""`` = the default DB), as ``saved-views.ts`` stores it."""

    name: str
    dataset: str
    spec: SearchSpec
