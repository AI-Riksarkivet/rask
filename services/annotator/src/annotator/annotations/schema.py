"""The annotations CONTRACT — request/response models, the table schema, identity.

One place for the shapes every annotations module shares. ``EMPTY_SCHEMA`` is the
single backend source of truth for the table's columns; ``scripts/seed_annotations.py``
imports it (identity columns prepended per descriptor) and a test asserts the seeded
dataset matches, so the schema cannot silently drift across its consumers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

import pyarrow as pa
from pydantic import BaseModel, Field


if TYPE_CHECKING:
    from service_kit.lancekit.descriptor import Declared

ANNOTATIONS_TABLE = "annotations"

#: The fields a reviewer may edit — the local-first review overlay flushed on save.
#: Only these are patched; geometry + provenance columns are carried forward from the
#: current row, so a partial edit never wipes a shape.
EDITABLE_FIELDS = ("label", "status", "text", "group", "reviewer")


class AnnotationEdit(BaseModel):
    """One reviewed annotation: its id + only the fields that changed."""

    id: str
    label: str | None = None
    status: str | None = None
    text: str | None = None
    group: str | None = None
    reviewer: str | None = None


class NewAnnotation(BaseModel):
    """A newly drawn shape. Geometry + attributes; the chunk identity columns are
    stamped server-side from the route keys, so the client sends only shape data.
    A human-drawn shape is ``accepted`` by construction, ``source=human``. These
    defaults are ALSO the row defaults for server-minted rows (chunk tags), so the
    contract lives in one model."""

    id: str
    shape_type: str
    x: float = 0.0
    y: float = 0.0
    width: float = 0.0
    height: float = 0.0
    rotation: float = 0.0
    polygon: list[float] = Field(default_factory=list)
    t_start: float = 0.0
    t_end: float = 0.0
    text: str = ""
    label: str = ""
    status: str = "accepted"
    source: str = "human"
    group: str = ""
    mask: str = ""


class GeometryEdit(BaseModel):
    """A moved/resized EXISTING shape — spatial geometry only, keyed by id (canvas drag)."""

    id: str
    x: float
    y: float
    width: float
    height: float
    polygon: list[float] = Field(default_factory=list)


class TemporalEdit(BaseModel):
    """A resized audio/video SEGMENT — its times only, keyed by id (waveform region drag)."""

    id: str
    t_start: float
    t_end: float


class SaveAnnotations(BaseModel):
    """The delta a Save flushes for one media unit: field edits + newly drawn shapes
    + moved geometry + deleted ids. Edits/inserts/geometry commit together (one
    merge_insert), deletes follow.

    ``base_version`` is the Lance version the client loaded — optimistic concurrency:
    the save 409s if the table advanced underneath it (someone else / a deriver wrote).
    """

    edits: list[AnnotationEdit] = Field(default_factory=list)
    inserts: list[NewAnnotation] = Field(default_factory=list)
    geometry: list[GeometryEdit] = Field(default_factory=list)
    temporal: list[TemporalEdit] = Field(default_factory=list)
    deletes: list[str] = Field(default_factory=list)
    base_version: int | None = None


class SaveResult(BaseModel):
    """One save. ``saved`` counts touched rows (edits+inserts+deletes)."""

    saved: int
    version: int


class TagWrite(BaseModel):
    """One chunk (or its unit) + the tag labels to set on it. ``keys`` are the NON-doc
    identity fields, positional — they pair with ``descriptor.identity.key_fields`` minus
    the doc key, exactly like ``identity_values`` / ``chunk_key_filter`` (a chunk tag
    sends ``keys=[speech_id, chunk_id]``)."""

    doc_id: str
    keys: list[int] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class TagBatch(BaseModel):
    """A workflow's chunk-tags promoted to annotation ROWS across many units, committed
    in ONE version. ``removes`` untags (deletes the deterministic tag rows). Author is
    stamped server-side; ``source``/``status`` are fixed for a human set."""

    adds: list[TagWrite] = Field(default_factory=list)
    removes: list[TagWrite] = Field(default_factory=list)
    base_version: int | None = None


#: The annotation contract — the schema of an EMPTY stream when a dataset has no
#: annotations table yet (so the client still parses). Aligned to the engine
#: (frontend/src/lib/engine/schema.ts) PLUS the active-learning columns
#: (confidence/uncertainty/source/model_version) so predictions round-trip and the
#: review queue can rank by them. THE single backend source of truth —
#: scripts/seed_annotations.py builds from this; tests assert the alignment.
#:
#: Settled while the table was still EMPTY (handoff Q1): ``created_at``/``updated_at``
#: are part of the contract and are stamped server-side on the save paths. The four
#: TRAINING columns — ``trained_in_version`` (int64 = Lance version), ``margin`` (f32),
#: ``logits`` (list/blob), ``encoder_embedding`` (list/blob) — are deliberately NOT
#: added here (handoff Q2): nothing defines or writes them anywhere yet, so they get
#: defined (additive evolution, never blocked) the moment the training loop first
#: needs them, not guessed now.
EMPTY_SCHEMA = pa.schema(
    [
        ("id", pa.string()),
        ("shape_type", pa.string()),
        ("x", pa.float32()),
        ("y", pa.float32()),
        ("width", pa.float32()),
        ("height", pa.float32()),
        ("rotation", pa.float32()),
        ("polygon", pa.list_(pa.float32())),
        # temporal facet — audio segments + a shape pinned to a video moment (seconds)
        ("t_start", pa.float32()),
        ("t_end", pa.float32()),
        ("text", pa.string()),
        # TEXTUAL facet — a span INTO another row's `text`, which is what token-classification and
        # the value half of KIE are made of. Nullable and additive: Lance assigns field IDs
        # incrementally, so fragments written before these existed are never rewritten and read back
        # as null (https://lance.org/format/table/schema/). Adding them is metadata-only.
        #
        # `parent_id` is the row the span sits inside, by ID — never an index. Rows renumber on every
        # reload, and an index would silently re-point at a different annotation.
        ("parent_id", pa.string()),
        ("char_start", pa.int32()),
        ("char_end", pa.int32()),
        ("label", pa.string()),
        ("status", pa.string()),
        ("source", pa.string()),
        ("reviewer", pa.string()),
        ("confidence", pa.float32()),
        ("uncertainty", pa.float32()),
        ("model_version", pa.string()),
        ("group", pa.string()),
        ("group_id", pa.string()),
        ("reading_order", pa.int32()),
        ("difficult", pa.bool_()),
        ("links", pa.string()),
        ("mask", pa.string()),
        ("metadata", pa.string()),
        # row lifecycle — stamped server-side (save/tags): both at row birth,
        # updated_at again on every edit. Timezone-aware UTC, microseconds.
        ("created_at", pa.timestamp("us", tz="UTC")),
        ("updated_at", pa.timestamp("us", tz="UTC")),
    ]
)


def identity_values(declared: Declared, doc_id: str, rest: Sequence[int]) -> dict[str, object]:
    """The chunk identity columns as a dict — the same (doc key, *other key fields)
    mapping ``chunk_key_filter`` builds as a predicate, stamped onto new rows so a
    drawn shape carries its unit's identity. Arity-generic off the descriptor.

    Deliberately TOLERANT of fewer ``rest`` values than key fields (the route paths
    pass a fixed tuple against possibly-narrower descriptors). Callers feeding
    CLIENT-supplied keys must validate arity first (``tags.check_keys_arity``) —
    a short list here would stamp NULL identity columns."""
    identity = declared.identity
    values: dict[str, object] = {identity.doc_key: doc_id}
    others = [f for f in identity.key_fields if f != identity.doc_key]
    for field, value in zip(others, rest, strict=False):
        values[field] = int(value)
    return values
