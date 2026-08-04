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
from typing import Any

import lance
from pydantic import BaseModel, Field, computed_field, model_validator

from service_kit.lancekit import store
from service_kit.lancekit.introspect import TableInfo, discover_tables


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
    """One SEARCHABLE table, with the bindings that make it searchable.

    Named, and a corpus may declare several. It was a single object, which meant a corpus could
    expose exactly one searchable table no matter how many it held — you could pick a DATASET but
    never a table within it.

    The shape mirrors `atlas`, which solved the same problem first: a named list, each entry bound to
    its own table. Display fields, fts and vector bindings are all per-TABLE, so they had to travel
    with the table rather than sit beside a single privileged one.
    """

    #: Stable key the UI and the `table=` selector use. `default` for a corpus that declares one,
    #: which is what every descriptor on disk says today.
    name: str = "default"
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
    #: Every searchable table this corpus exposes, in declaration order. The FIRST is the default.
    searches: list[Search] = Field(default_factory=list)
    atlas: list[AtlasSpace] = Field(default_factory=list)
    capabilities: dict[str, str] = Field(default_factory=dict)  # capability → table/column it needs

    @model_validator(mode="before")
    @classmethod
    def _accept_single_search(cls, data: Any) -> Any:
        """`search: {...}` (what every descriptor on disk carries) becomes `searches: [{...}]`.

        A before-validator and not a migration script: descriptors are FILES, written by hand and by
        seeding scripts, and rewriting them all to land one feature would make the feature's blast
        radius the whole corpus estate. `extra="allow"` on this model makes the translation
        load-bearing rather than cosmetic — without popping the key, `search` would survive as an
        untyped extra and quietly shadow nothing while the real list stayed empty.
        """
        if not isinstance(data, dict):
            return data
        legacy = data.get("search")
        if legacy is None:
            return data
        data = dict(data)
        data.pop("search")
        # An explicit `searches` wins: a descriptor carrying both is stating the new shape, and the
        # legacy key is then a leftover rather than an instruction.
        data.setdefault("searches", [legacy])
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def search(self) -> Search | None:
        """The DEFAULT searchable table, or None.

        A property, so the ~15 existing readers of `declared.search` keep working untouched — and a
        COMPUTED field, so the served JSON still carries `search` beside the new `searches`. The
        frontend reads `declared.search` today; dropping it from the wire to add a list would have
        broken every zone in the same commit that added the capability.
        """
        return self.searches[0] if self.searches else None

    def search_named(self, name: str | None) -> Search | None:
        """One declared table by name — `None` selects the default.

        Returns `None` for a name this corpus does not declare, which callers must treat as a 404
        rather than silently falling back to the default: a search that quietly answered from a
        different table than the one asked for would be a wrong answer, not a lenient one.
        """
        if name is None:
            return self.search
        return next((s for s in self.searches if s.name == name), None)


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

    # EVERY declared searchable table, not just the default. A corpus can now expose several, and a
    # second one whose row table does not exist would otherwise load fine and 500 the first time
    # anyone selected it.
    seen_names: set[str] = set()
    for search in declared.searches:
        if search.name in seen_names:
            # Names are the `table=` selector's keys, so a duplicate makes the selector ambiguous —
            # and the one it resolves to would depend on declaration order, invisibly.
            problems.append(f"search {search.name!r} is declared twice")
        seen_names.add(search.name)

        info = tables.get(search.row_table)
        if info is None:
            problems.append(f"search {search.name!r}: row_table {search.row_table!r} does not exist")
            continue
        for key in declared.identity.key_fields:
            if info.column(key) is None:
                problems.append(f"identity field {key!r} missing from {search.row_table!r}")

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

    for search in declared.searches:
        for mode, binding in search.vectors.items():
            info = tables.get(binding.table)
            column = info.column(binding.column) if info else None
            if column is None:
                problems.append(f"vector binding {mode!r}: {binding.table}.{binding.column} missing")
            elif column.vector_dim != binding.dim:
                problems.append(f"vector binding {mode!r}: {binding.table}.{binding.column} dim {column.vector_dim} != declared {binding.dim}")
        if search.fts is not None:
            info = tables.get(search.fts.table)
            if info is None or info.column(search.fts.column) is None:
                problems.append(f"fts binding: {search.fts.table}.{search.fts.column} missing")

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
