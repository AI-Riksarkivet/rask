"""The TWO bronze writers cannot drift apart silently (#99).

The ingest plane superseded the medallion's IIIF head without inheriting #92's fixity column — and
nothing failed, because no test related the two schemas. That is the same failure class as the
`stage` postmortem in `ingest/runtime.py`: a column difference between writers is only cheap until
something projects it (the viewer's `_PAGE_COLUMNS`, the movers' carry-forward, a fixity audit).
This suite pins the relationship: every column the two writers SHARE agrees on type, and the
governed core — id, source_uri, payload, sha256, stage — is present in both, so a row's provenance
story reads the same whichever head landed it.
"""

from __future__ import annotations

import pyarrow as pa
from ingest.runtime import BRONZE_SCHEMA
from medallion.services.ingest import _ingest_schema  # noqa: PLC2701 — the writer's real schema, not a copy


#: What every bronze page row must carry regardless of which plane wrote it: identity, provenance,
#: the bytes, their fixity, and the tier stamp.
_GOVERNED_CORE = {"id", "source_uri", "payload", "sha256", "stage"}


def test_the_governed_core_is_present_in_BOTH_writers() -> None:
    medallion_schema = _ingest_schema(None)
    for name in sorted(_GOVERNED_CORE):
        assert name in BRONZE_SCHEMA.names, f"ingest plane bronze lacks {name!r}"
        assert name in medallion_schema.names, f"medallion bronze lacks {name!r}"


def test_shared_columns_agree_on_type() -> None:
    """Same name, same type — a reader that projects a shared column must not care which head wrote
    the dataset. Compared as name→type (column ORDER already differs between the writers and no
    reader depends on it)."""
    medallion_schema = _ingest_schema(None)
    ingest_types = {f.name: f.type for f in BRONZE_SCHEMA}
    medallion_types = {f.name: f.type for f in medallion_schema}
    shared = set(ingest_types) & set(medallion_types)
    assert shared >= _GOVERNED_CORE
    for name in sorted(shared):
        assert ingest_types[name] == medallion_types[name], f"column {name!r} differs: ingest={ingest_types[name]} medallion={medallion_types[name]}"


def test_the_fixity_column_is_a_string_in_both() -> None:
    """The digest is a hex STRING (the medallion's #92 shape) — not binary, not nullable-by-accident
    in one plane only. Pinned separately because fixity is the column #99 exists for."""
    assert BRONZE_SCHEMA.field("sha256").type == pa.string()
    assert _ingest_schema(None).field("sha256").type == pa.string()
