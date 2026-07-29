"""Atlas ``/points`` payload builder — the Arrow IPC serialization behind the map.

Ported from the pre-split ``backend/atlas/points.py`` onto the descriptor: the
space's x/y/cluster columns and source table come from ``declared.atlas``, the
per-point keys from ``declared.identity``, the categorical colour channels from
``declared.search.filterable`` (those that exist on the space's table), and the
per-doc labels (``docFiles`` metadata) from the first ``declared.display.title``
field present on the table. Pure data path (no HTTP): scan the projected
columns and encode one Apache Arrow IPC stream (float16 coords + int keys + a
few ``DICTIONARY<int32, utf8>`` colour columns). :mod:`viewer.api.v1.endpoints.atlas`
wires this to the ``/api/atlas`` routes and memoizes the bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from service_kit.exceptions import ValidationError
from service_kit.lancekit.descriptor import AtlasSpace, Declared


def _dictionary(column: pa.ChunkedArray) -> pa.Array:
    """Encode a column as Arrow ``DICTIONARY<int32, utf8>`` — the codes/labels
    split done natively (indices = per-point colour codes, values = labels).

    NULLs are filled to the empty-string label ``""`` first (the frontend
    renders that muted), so the dictionary carries no nulls. ``int32`` indices
    keep the codes in the typed-array range the JS scatter expects.
    """
    filled = pc.fill_null(column.cast(pa.string()), "")
    encoded = filled.combine_chunks().dictionary_encode()
    return encoded.cast(pa.dictionary(pa.int32(), pa.string()))


def _label_field(declared: Declared, present: set[str]) -> str | None:
    """The per-doc label source: first declared title field on this table that
    isn't the doc key itself (None → the doc key doubles as the label)."""
    doc_key = declared.identity.doc_key
    return next((f for f in declared.display.title if f != doc_key and f in present), None)


def _doc_labels(doc_dict: pa.Array, doc_values: list[str], label_values: list[str | None] | None) -> list[str]:
    """One label per distinct doc, aligned with the ``doc`` dictionary order.

    The map view colours/labels by document using a readable stem rather than
    the hashed doc key. The result is one value per dictionary entry, in
    dictionary order, so the frontend can index it by the same code it uses
    for ``doc``. Shipped in schema metadata (not a column: it has one entry
    per distinct doc, not one per point).
    """
    labels = doc_dict.dictionary.to_pylist()
    if label_values is None:
        return [str(d) for d in labels]
    first: dict[str, str] = {}
    for d, v in zip(doc_values, label_values, strict=True):
        if d not in first:
            first[d] = Path(str(v)).stem if v else d
    return [first.get(d, d) for d in labels]


def _resolve_channels(space: AtlasSpace, present: set[str]) -> list[tuple[str, str]]:
    """Declared atlas channels resolved to ``(output_name, source_column)`` pairs.

    A ``broadest_prefix`` channel maps to the highest-numbered ``<prefix>N``
    column present (the broadest topic layer — index is data-dependent, so no
    layer literal lives in code). Channels whose source column is absent are
    dropped.
    """
    resolved: list[tuple[str, str]] = []
    for channel in space.channels:
        if channel.column is not None:
            if channel.column in present:
                resolved.append((channel.name, channel.column))
        elif channel.broadest_prefix is not None:
            prefix = channel.broadest_prefix
            layers = sorted(
                (c for c in present if c.startswith(prefix) and c[len(prefix) :].isdigit()),
                key=lambda c: int(c[len(prefix) :]),
            )
            if layers:
                resolved.append((channel.name, layers[-1]))
    return resolved


def build_points(declared: Declared, space: AtlasSpace, ds: lance.LanceDataset) -> bytes:
    """The expensive part of /points: full-table scan → one Arrow IPC stream.

    Builds a single ``pyarrow.Table`` (float16 coords, ~3 sig-digit precision —
    fine for a ~2000px scatter, and it halves both the wire payload and the GPU
    vertex buffer; int32/int64 keys, and the descriptor-declared categorical
    channels as ``DICTIONARY<int32, utf8>``) and serializes it to Arrow IPC
    **stream** bytes. Pulled out so the router can memoize the bytes per
    (dataset, space, version).
    """
    schema = ds.schema
    present = set(schema.names)
    identity = declared.identity
    if identity.doc_key not in present:
        raise ValidationError(f"atlas table lacks the doc key column '{identity.doc_key}'")
    other_keys = [k for k in identity.key_fields if k != identity.doc_key and k in present]
    label_field = _label_field(declared, present)
    cluster = space.cluster if space.cluster in present else None
    # Declared atlas channels (output name → source column) resolved against the
    # live schema; a broadest-prefix channel picks the highest-numbered layer
    # column present. Falls back to the declared filterable fields (legacy
    # behavior) when a space declares no channels.
    resolved = _resolve_channels(space, present)
    if not resolved:
        filterable = declared.search.filterable if declared.search is not None else []
        resolved = [(f, f) for f in filterable if f in present and f != identity.doc_key]

    source_cols = [src for _, src in resolved]
    wanted = [identity.doc_key, label_field, *other_keys, space.x, space.y, cluster, *source_cols]
    columns = [c for c in dict.fromkeys(wanted) if c is not None]

    # `with_row_id` ships each point's stable Lance row address (`_rowid`) so the
    # selection table can be fetched with an O(selection) `take` (see /chunks)
    # instead of a per-key filtered full-table scan.
    tbl = ds.scanner(columns=columns, filter=f"{space.x} IS NOT NULL", with_row_id=True).to_table()

    def halves(name: str) -> pa.Array:
        # Ship coords as float16 — ~3 sig-digit precision, sub-pixel on the
        # ~2000px scatter — halving the /points payload AND the GPU vertex
        # buffer. The frontend reads the raw f16 bits for the GPU and decodes
        # them to f32 for CPU hover/lasso math.
        arr = tbl.column(name).to_numpy(zero_copy_only=False).astype(np.float16)
        return pa.array(arr, type=pa.float16())

    def ints(name: str, dtype: pa.DataType) -> pa.Array:
        return tbl.column(name).combine_chunks().cast(dtype)

    doc_dict = _dictionary(tbl.column(identity.doc_key))
    label_values = tbl.column(label_field).to_pylist() if label_field else None
    doc_files = _doc_labels(doc_dict, tbl.column(identity.doc_key).to_pylist(), label_values)

    arrays: list[pa.Array] = [halves(space.x), halves(space.y)]
    names: list[str] = ["x", "y"]
    for key in other_keys:
        field = schema.field(key)
        if pa.types.is_integer(field.type):
            arrays.append(ints(key, pa.int32()))
        else:
            arrays.append(_dictionary(tbl.column(key)))
        names.append(key)
    # Stable address for take-based selection fetch.
    arrays.append(ints("_rowid", pa.int64()))
    names.append("rowid")
    # DICTIONARY<int32, utf8>: indices = `doc`, values = the distinct doc keys.
    arrays.append(doc_dict)
    names.append("doc")
    if cluster is not None:
        arrays.append(ints(cluster, pa.int32()))
        names.append("cluster")
    for out_name, source_col in resolved:
        if out_name in names:
            continue
        # Declared categorical channel — low-cardinality metadata for
        # legend/hover. A small label list + a per-point int32; high-cardinality
        # text stays out and is lazy-fetched per chunk via /atlas/chunk.
        arrays.append(_dictionary(tbl.column(source_col)))
        names.append(out_name)

    # count + space + docFiles ride along in the schema metadata. `docFiles` has
    # one entry per distinct doc (not one per point), so it can't be a column in
    # this per-point table — it's JSON-encoded here, aligned with the `doc`
    # dictionary order so the frontend can index it by the same code.
    out_schema = pa.schema(
        [pa.field(n, a.type) for n, a in zip(names, arrays, strict=True)],
        metadata={
            b"count": str(tbl.num_rows).encode(),
            b"space": space.name.encode(),
            b"docFiles": json.dumps(doc_files).encode(),
        },
    )
    out = pa.table(arrays, schema=out_schema)

    sink = pa.BufferOutputStream()
    with pa.ipc.RecordBatchStreamWriter(sink, out.schema) as writer:
        writer.write_table(out)
    return sink.getvalue().to_pybytes()
