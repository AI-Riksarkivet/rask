"""The per-unit review Save — the local-first write path (NOT sync-per-edit).

Edits accumulate client-side (in-memory overlay + undo/redo); one Save patches only
the editable fields onto the current rows (geometry/provenance carried forward) and
``merge_insert("id")`` commits them as ONE atomic new version — the lakehouse
"version IS the handshake". (The catalog-governed write path is the merge step; this
is the direct-write prototype, mirroring the direct read.)
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pyarrow as pa
from fastapi import APIRouter

from annotator.annotations.commit import check_base_version_value, delete_by_ids, finalize_commit
from annotator.annotations.schema import (
    ANNOTATIONS_TABLE,
    EDITABLE_FIELDS,
    NewAnnotation,
    SaveAnnotations,
    SaveResult,
    identity_values,
)
from annotator.api.security import CurrentSubject, RawBearerToken
from service_kit.lancekit.keys import chunk_key_filter, validate_doc_key
from service_kit.lancekit.reader import open_reader
from service_kit.lancekit.registry import table_dataset
from service_kit.lancekit.writer import open_writer
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import dataset_handle


logger = logging.getLogger(__name__)

router = APIRouter(tags=["annotate"])


def build_delta(current: pa.Table, edits_by_id: dict[str, dict[str, object]]) -> pa.Table:
    """The merge_insert source: current rows for the edited ids, editable fields
    patched, everything else (geometry, provenance) carried forward. Same schema as
    ``current`` so merge_insert updates in place."""
    patched = [{**row, **edits_by_id[row["id"]]} for row in current.to_pylist() if row["id"] in edits_by_id]
    return pa.Table.from_pylist(patched, schema=current.schema)


def new_rows(inserts: Sequence[NewAnnotation], ident: Mapping[str, object], schema: pa.Schema) -> pa.Table:
    """Full new-annotation rows: identity stamped + shape fields; columns absent from
    the payload fall to null via the schema. Same schema ⇒ merge_insert can insert."""
    rows = [{**ident, **ins.model_dump()} for ins in inserts]
    return pa.Table.from_pylist(rows, schema=schema)


@router.post("/annotations/{doc_id}/{speech_id}/{chunk_id}")
def save_annotations(
    state: StateDep,
    # The VERIFIED subject, never a header (docs/DECISIONS.md "The Python estate audit": any caller could sign another
    # person's name onto annotation provenance via `X-User`). `current_subject` is `anon` with
    # OIDC off (byte-identical dev behaviour), the token's `sub` with it on, 401 with no token,
    # 503 when enabled-but-unwired — the same no-header-fallback rule the projects plane got.
    author: CurrentSubject,
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    body: SaveAnnotations,
    caller_token: RawBearerToken = None,
    dataset: DatasetParam = None,
) -> SaveResult:
    """Flush a review delta to Lance as ONE atomic version (see module docstring)."""
    handle = dataset_handle(state, dataset)
    declared = handle.descriptor.declared
    doc_id = validate_doc_key(declared, doc_id)
    # Full catalog mode never opens the local replica (the catalog 404s absent
    # tables through the error translation); any other config keeps local resolution.
    ds = None if state.settings.rest_catalog_mode else table_dataset(handle, ANNOTATIONS_TABLE)
    # The version check AND the carry-forward read both go through the reader seam:
    # in catalog mode the catalog table is the truth (patching against a stale local
    # copy would resurrect old fields), and the 409 compares the version the client
    # loaded against the same source the wire GET served it from.
    table_id = state.settings.catalog_table_id(handle.id, ANNOTATIONS_TABLE)
    reader = open_reader(dataset=ds, table_id=table_id, settings=state.settings, caller_token=caller_token)
    # REQUIRED here (#50): this path commits a merge_upsert, which UPDATES matched rows, so a save
    # that does not state what it was built from can overwrite another annotator's fields. The tag
    # batch is insert-only and keeps the precondition optional — see check_base_version_value.
    check_base_version_value(reader.table_version(), body.base_version, required=True)

    where = chunk_key_filter(declared, doc_id, (speech_id, chunk_id))
    current = reader.to_table(filter=where)

    # edits (patch existing) + inserts (new shapes) commit in ONE merge_insert; the
    # source is the union, keyed by id — matched ⇒ update, unmatched ⇒ insert.
    edits_by_id: dict[str, dict[str, object]] = {e.id: e.model_dump(include=set(EDITABLE_FIELDS), exclude_none=True) for e in body.edits}
    # Geometry moves + segment-time resizes patch the same rows (by id) — merged in.
    for g in body.geometry:
        edits_by_id.setdefault(g.id, {}).update({"x": g.x, "y": g.y, "width": g.width, "height": g.height, "polygon": g.polygon})
    for tseg in body.temporal:
        edits_by_id.setdefault(tseg.id, {}).update({"t_start": tseg.t_start, "t_end": tseg.t_end})
    # Re-anchored text spans (the transcription-edit remap) patch offsets the same way.
    for span in body.spans:
        edits_by_id.setdefault(span.id, {}).update({"char_start": span.char_start, "char_end": span.char_end})
    # Governance: the SERVER stamps who wrote each touched row (not the client's claim) —
    # the per-user seam. lance-ns's OpenFGA keys on this author at merge. Row lifecycle
    # is stamped the same way: updated_at on every touched row, created_at at row birth
    # (build_delta/new_rows drop the keys when a pre-settle table lacks the columns).
    now = datetime.now(UTC)
    for fields in edits_by_id.values():
        fields["reviewer"] = author
        fields["updated_at"] = now
    parts = [build_delta(current, edits_by_id)]
    if body.inserts:
        ident = {
            **identity_values(declared, doc_id, (speech_id, chunk_id)),
            "reviewer": author,
            "created_at": now,
            "updated_at": now,
        }
        parts.append(new_rows(body.inserts, ident, current.schema))
    delta = pa.concat_tables(parts)

    # Writes flow through the writer seam (direct default = byte-identical; catalog
    # merge_insert/delete at merge, which yields OpenFGA + OpenLineage for free).
    writer = open_writer(dataset=ds, table_id=table_id, settings=state.settings, caller_token=caller_token)
    touched = 0
    if delta.num_rows:
        writer.merge_upsert(delta, "id")
        touched += delta.num_rows
    if body.deletes:
        delete_by_ids(writer, body.deletes)
        touched += len(body.deletes)

    result = finalize_commit(
        handle,
        state.settings,
        touched=touched,
        unit_key=f"{doc_id}/{speech_id}/{chunk_id}",
        caller_token=caller_token,
    )
    if touched:
        logger.info(
            "saved %s→ v%d (%d edit+insert, %d delete)",
            doc_id,
            result.version,
            delta.num_rows,
            len(body.deletes),
        )
    return result
