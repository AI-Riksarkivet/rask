"""The shared commit choreography — one write ritual for every annotations save path.

Both save routes (the per-unit review Save and the batch tag write) perform the same
sequence around their different deltas: optimistic-concurrency check → write via the
writer seam → delete-by-ids → re-open → OpenLineage emit → SaveResult. Extracted here
so the ritual exists ONCE and a change (e.g. the catalog-governed write at merge)
lands in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from annotator.annotations.schema import ANNOTATIONS_TABLE, SaveResult
from service_kit.exceptions import ConflictError
from service_kit.lancekit.lineage_emit import emit_save
from service_kit.lancekit.predicate import isin
from service_kit.lancekit.reader import open_reader
from service_kit.lancekit.registry import table_dataset


if TYPE_CHECKING:
    from collections.abc import Sequence

    from service_kit.lancekit.registry import DatasetHandle
    from service_kit.lancekit.writer import TableWriter
    from service_kit.media.config import Settings


def check_base_version_value(current: int, base_version: int | None) -> None:
    """Optimistic concurrency, source-agnostic: 409 if the table advanced past the
    version the client loaded — ``current`` may be a direct ``ds.version`` or the
    catalog's version primitive. ``None`` skips it (last-write-wins per id)."""
    if base_version is not None and base_version != current:
        raise ConflictError(f"annotations changed on the server (loaded v{base_version}, now v{current})")


def delete_by_ids(writer: TableWriter, ids: Sequence[str]) -> None:
    """Delete rows by id through the writer seam — quoted, injection-guarded."""
    writer.delete(isin("id", ids))


def finalize_commit(
    handle: DatasetHandle,
    settings: Settings,
    *,
    touched: int,
    unit_key: str,
) -> SaveResult:
    """Close out a write: report the post-write version FROM THE READER SEAM and
    emit the pre-merge OpenLineage RunEvent. In full catalog mode no local table is
    touched — the version comes from the catalog and the catalog emits the RunEvent
    for the same write (``effective_lineage_sink`` is ``none`` there)."""
    table_id = settings.catalog_table_id(handle.id, ANNOTATIONS_TABLE)
    fresh = None if settings.rest_catalog_mode else table_dataset(handle, ANNOTATIONS_TABLE)
    reader = open_reader(dataset=fresh, table_id=table_id, settings=settings)
    version = reader.table_version()
    sink = settings.effective_lineage_sink
    if touched and sink != "none" and fresh is not None:
        emit_save(
            ds=fresh,
            table_uri=handle.table_uri(ANNOTATIONS_TABLE),
            table_name=ANNOTATIONS_TABLE,
            unit_key=unit_key,
            sink=sink,
        )
    return SaveResult(saved=touched, version=version)
