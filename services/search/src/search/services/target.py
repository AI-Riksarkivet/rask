"""Descriptor → runtime search target: the one place table/column names resolve.

``resolve_target`` turns a :class:`~common.lancekit.registry.DatasetHandle`
into a :class:`SearchTarget` holding the opened Lance handles plus every name
the retrieval modules need (key fields, hit projection, bindings). Nothing
downstream of this module reads the descriptor, so the "all corpus knowledge
flows from the descriptor" rule (§4.4) is enforced structurally.
"""

from __future__ import annotations

import logging
from typing import Any

from common.core.exceptions import ValidationError
from common.lancekit.descriptor import Declared, FtsBinding, VectorBinding
from common.lancekit.introspect import TableInfo
from common.lancekit.registry import DatasetHandle
from pydantic import BaseModel, ConfigDict

from search.services.constants import DURATION_COLUMN
from search.services.filters import topic_layer_columns
from search.services.spec import SearchMode


logger = logging.getLogger(__name__)

# Capability key naming the per-hit word-timing column ("table.column" target).
_ALIGNMENTS_CAPABILITY = "alignments"


def hit_columns(declared: Declared, row_info: TableInfo) -> list[str]:
    """Derive the hit projection for a row table from its declared roles.

    Identity keys + time bounds (+ the legacy ``duration`` convenience column)
    + display body/title/metadata fields — deduped, and intersected with the
    table's live columns so a declared-but-absent display field can never break
    a ``select``.
    """
    display = declared.display
    wanted: list[str] = list(declared.identity.key_fields)
    if declared.time is not None:
        wanted += [declared.time.start, declared.time.end]
    wanted.append(DURATION_COLUMN)
    if display.body:
        wanted.append(display.body)
    wanted += display.title
    wanted += [m.field for m in display.metadata]

    live = {c.name for c in row_info.columns}
    out: list[str] = []
    for name in wanted:
        if name in live and name not in out:
            out.append(name)
    return out


class SearchTarget(BaseModel):
    """Everything the retrieval modules need, resolved once per request.

    The lancedb/lance handles aren't Pydantic-validatable, hence
    ``arbitrary_types_allowed`` and the ``Any`` slots.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    row_table_name: str
    key_fields: list[str]
    payload_columns: list[str]
    filterable: list[str]
    topic_columns: list[str]
    fts: FtsBinding | None
    vectors: dict[str, VectorBinding]
    # Column popped into each hit's ``alignments`` (None when the capability is
    # absent or points off the row table — hits then carry ``alignments: []``).
    alignments_column: str | None
    # Text column standing in for the scene vector's space (the frame caption);
    # attached to every hit for the list/table views.
    caption_column: str | None
    # The display body column — what the cross-encoder reranker scores.
    body_column: str | None
    row_tbl: Any  # lancedb sync Table
    row_ds: Any  # row_tbl.to_lance(), resolved once per request
    tables: dict[str, Any]  # opened lancedb Tables for every vector-binding table

    def table_for(self, name: str) -> Any | None:
        return self.tables.get(name)

    def binding(self, mode: str) -> VectorBinding | None:
        """The vector binding declared under ``mode`` (a vector-space key), or
        None. ``str`` so any declared embedding key resolves, not just the named
        roles — ``SearchMode`` members are strings, so they still work."""
        return self.vectors.get(str(mode))

    def caption_table(self) -> Any | None:
        """The frame table carrying ``caption_column``, or None."""
        scene = self.vectors.get(SearchMode.SCENE.value)
        if scene is None or not scene.caption_source:
            return None
        return self.table_for(scene.table)


def resolve_target(handle: DatasetHandle) -> SearchTarget:
    """Open the search tables named by ``handle``'s descriptor.

    Raises :class:`ValidationError` (HTTP 400) when the dataset declares no
    search block — an unsearchable dataset is a client addressing error, not a
    server fault. A vector-binding table that fails to open degrades that leg
    to empty instead of failing every mode.
    """
    declared = handle.descriptor.declared
    search = declared.search
    if search is None:
        raise ValidationError("dataset has no search bindings")

    # Open FIRST, then consult the cached descriptor: the open is ground truth,
    # and syncing it into the cache heals the stale-handle race (a row table
    # rebuilt or created after the dataset was first opened). Only a genuinely
    # missing table maps to 400 — infra faults (S3, corrupt manifest) stay 500s.
    try:
        row_tbl = handle.db.open_table(search.row_table)
    except (ValueError, FileNotFoundError, OSError) as e:
        msg = str(e).lower()
        if "not found" in msg or "does not exist" in msg:
            raise ValidationError("search row table missing from dataset") from e
        raise
    handle.sync_table_info(search.row_table, int(row_tbl.version))
    row_info = handle.descriptor.tables[search.row_table]
    tables: dict[str, Any] = {search.row_table: row_tbl}
    for binding in search.vectors.values():
        if binding.table in tables:
            continue
        try:
            tables[binding.table] = handle.db.open_table(binding.table)
        except Exception:
            logger.warning("vector-binding table %s failed to open", binding.table, exc_info=True)

    alignments_column: str | None = None
    alignments_ref = declared.capabilities.get(_ALIGNMENTS_CAPABILITY)
    if alignments_ref:
        table, _, column = alignments_ref.partition(".")
        # Only a row-table column can ride on hits; a foreign-table target would
        # make the pop strip an unrelated same-named hit field.
        if column and table == search.row_table:
            alignments_column = column

    scene = search.vectors.get(SearchMode.SCENE.value)

    return SearchTarget(
        row_table_name=search.row_table,
        key_fields=list(declared.identity.key_fields),
        payload_columns=hit_columns(declared, row_info),
        filterable=list(search.filterable),
        topic_columns=topic_layer_columns([c.name for c in row_info.columns]),
        fts=search.fts,
        vectors=dict(search.vectors),
        alignments_column=alignments_column,
        caption_column=scene.caption_source if scene else None,
        body_column=declared.display.body,
        row_tbl=row_tbl,
        row_ds=row_tbl.to_lance(),
        tables=tables,
    )
