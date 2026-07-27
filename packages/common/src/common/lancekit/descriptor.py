"""The dataset descriptor — declared semantic roles + discovered facts, merged.

The *declared* half (LANCE_MEDIA_MERGE §4.2) carries what introspection cannot
know: which fields form row identity, which table/column serve the media
bytes, what to display, how search modes bind to vector columns. It is DATA —
read from a config file in ``config/descriptors/<dataset>.json`` (override,
for datasets we can't rewrite) or from the ``lance_media.descriptor`` schema-
metadata key stamped at table-create time.

:func:`validate_descriptor` enforces the load-bearing invariants against the
live schema at load time (and in tests): identity fields must exist, the media
blob column must really be blob-v2, every vector binding must name an actual
FixedSizeList column with the declared dimension.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import lance
from pydantic import BaseModel, Field

from common.lancekit import store
from common.lancekit.introspect import TableInfo, discover_tables


logger = logging.getLogger(__name__)

DESCRIPTOR_METADATA_KEY = b"lance_media.descriptor"


class Identity(BaseModel):
    """Row identity: the key fields (in order) and the doc-key whitelist pattern."""

    key_fields: list[str]
    doc_key: str = "doc_id"
    doc_key_pattern: str = r"^[A-Za-z0-9_-]{1,64}$"


class DocumentBinding(BaseModel):
    """Where a document's media bytes and thumbnail live."""

    table: str
    media_blob: str
    mime: str | None = None
    thumbnail: str | None = None
    thumbnail_mime: str | None = None


class TimeBinding(BaseModel):
    start: str
    end: str


class MetadataField(BaseModel):
    field: str
    label: str


class Display(BaseModel):
    title: list[str] = Field(default_factory=list)
    body: str | None = None
    caption: str | None = None
    metadata: list[MetadataField] = Field(default_factory=list)


class VectorBinding(BaseModel):
    """One searchable vector column and how a query reaches its space."""

    table: str
    column: str
    dim: int
    query_encoder: str  # "text" | "image" — which encoder embeds the query
    caption_source: str | None = None  # FTS-able text column in the same space, if any


class FtsBinding(BaseModel):
    table: str
    column: str
    language: str = "English"


class Search(BaseModel):
    row_table: str
    fts: FtsBinding | None = None
    vectors: dict[str, VectorBinding] = Field(default_factory=dict)  # mode name → binding
    filterable: list[str] = Field(default_factory=list)
    rerank: bool = False


class AtlasChannel(BaseModel):
    """One categorical colour channel on the atlas: output name ← source column.

    ``column`` names the source column directly; ``broadest_prefix`` instead
    selects the highest-numbered ``<prefix>N`` column present (the broadest
    topic layer, whose index is data-dependent) — so no ``topic_l`` literal
    lives in code. Exactly one of the two is set.
    """

    name: str
    column: str | None = None
    broadest_prefix: str | None = None


class AtlasSpace(BaseModel):
    name: str
    x: str
    y: str
    cluster: str
    source_column: str
    table: str
    channels: list[AtlasChannel] = Field(default_factory=list)


class Declared(BaseModel):
    """The declared half of a descriptor. ``extra`` kept for forward compat."""

    model_config = {"extra": "allow"}

    identity: Identity
    document: DocumentBinding | None = None
    time: TimeBinding | None = None
    display: Display = Display()
    search: Search | None = None
    atlas: list[AtlasSpace] = Field(default_factory=list)
    capabilities: dict[str, str] = Field(default_factory=dict)  # capability → table/column it needs


class DatasetDescriptor(BaseModel):
    """Merged view served to clients: dataset id + discovered tables + declared roles."""

    id: str
    tables: dict[str, TableInfo]
    declared: Declared

    def capability_available(self, name: str) -> bool:
        target = self.declared.capabilities.get(name)
        if target is None:
            return False
        table, _, column = target.partition(".")
        info = self.tables.get(table)
        if info is None:
            return False
        return column == "" or info.column(column) is not None


def load_declared(
    db_path: str | Path,
    dataset_id: str,
    descriptor_dir: str | Path,
    storage_options: dict[str, str] | None = None,
) -> Declared | None:
    """Config-file override first, else the schema-metadata stamp on any table."""
    override = Path(descriptor_dir) / f"{dataset_id}.json"
    if override.exists():
        return Declared.model_validate_json(override.read_text())
    for stem in store.list_lance_stems(db_path, storage_options):
        uri = store.join(db_path, f"{stem}.lance")
        metadata = lance.dataset(uri, storage_options=storage_options).schema.metadata or {}
        raw = metadata.get(DESCRIPTOR_METADATA_KEY)
        if raw:
            return Declared.model_validate(json.loads(raw.decode()))
    return None


def validate_descriptor(declared: Declared, tables: dict[str, TableInfo]) -> list[str]:
    """The P2.2 cross-check: declared roles must exist in the live schema.

    Returns the list of violations (empty = valid). Callers decide whether to
    raise; the loader logs and refuses to serve an invalid descriptor.
    """
    problems: list[str] = []

    row_table = declared.search.row_table if declared.search else None
    if row_table:
        info = tables.get(row_table)
        if info is None:
            problems.append(f"search.row_table {row_table!r} does not exist")
        else:
            for key in declared.identity.key_fields:
                if info.column(key) is None:
                    problems.append(f"identity field {key!r} missing from {row_table!r}")

    if declared.document is not None:
        doc_table = tables.get(declared.document.table)
        if doc_table is None:
            problems.append(f"document.table {declared.document.table!r} does not exist")
        else:
            blob_col = doc_table.column(declared.document.media_blob)
            if blob_col is None:
                problems.append(f"document.media_blob {declared.document.media_blob!r} missing")
            elif not blob_col.is_blob:
                problems.append(f"document.media_blob {declared.document.media_blob!r} is not a lance.blob.v2 column")

    if declared.search is not None:
        for mode, binding in declared.search.vectors.items():
            info = tables.get(binding.table)
            column = info.column(binding.column) if info else None
            if column is None:
                problems.append(f"vector binding {mode!r}: {binding.table}.{binding.column} missing")
            elif column.vector_dim != binding.dim:
                problems.append(f"vector binding {mode!r}: {binding.table}.{binding.column} dim {column.vector_dim} != declared {binding.dim}")
        if declared.search.fts is not None:
            info = tables.get(declared.search.fts.table)
            if info is None or info.column(declared.search.fts.column) is None:
                problems.append(f"fts binding: {declared.search.fts.table}.{declared.search.fts.column} missing")

    for space in declared.atlas:
        info = tables.get(space.table)
        for col in (space.x, space.y, space.cluster):
            if info is None or info.column(col) is None:
                problems.append(f"atlas space {space.name!r}: column {col!r} missing from {space.table!r}")

    return problems


def load_dataset_descriptor(
    db_path: str | Path,
    dataset_id: str,
    descriptor_dir: str | Path,
    storage_options: dict[str, str] | None = None,
) -> DatasetDescriptor:
    """Discover + declare + validate; raises ``ValueError`` on an invalid descriptor."""
    tables = discover_tables(db_path, storage_options)
    declared = load_declared(db_path, dataset_id, descriptor_dir, storage_options)
    if declared is None:
        raise ValueError(f"dataset {dataset_id!r}: no declared descriptor (config file or schema stamp)")
    problems = validate_descriptor(declared, tables)
    if problems:
        raise ValueError(f"dataset {dataset_id!r}: invalid descriptor: " + "; ".join(problems))
    return DatasetDescriptor(id=dataset_id, tables=tables, declared=declared)
