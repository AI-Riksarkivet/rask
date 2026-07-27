"""#74 tail read-back: the catalog reads table schema-metadata so the Table Properties UI can display it.

pylance 8.0.0's describe_table leaves DescribeTableResponse.metadata empty, so the editor seeded blank
(browser-driven find 2026-07-21). ``read_schema_metadata`` fills it from the dataset's Arrow schema metadata,
decoding bytes and excluding internal ``lineage.*`` bookkeeping keys.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pyarrow as pa
import pytest
from catalog.services import dataplane


def _patch_ds(monkeypatch: pytest.MonkeyPatch, schema: pa.Schema) -> None:
    monkeypatch.setattr(dataplane, "open_dataset", lambda *_a, **_k: SimpleNamespace(schema=schema))


def test_reads_and_decodes_user_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    schema = pa.schema(
        [pa.field("id", pa.int64())],
        metadata={b"owner": b"data-eng", b"tier": b"gold"},
    )
    _patch_ds(monkeypatch, schema)
    out = dataplane.read_schema_metadata(cast(Any, None), cast(Any, {}), ["db", "t"])
    assert out == {"owner": "data-eng", "tier": "gold"}


def test_excludes_internal_lineage_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lineage.* keys are catalog/lineage bookkeeping stamped at create — not user properties.
    schema = pa.schema(
        [pa.field("id", pa.int64())],
        metadata={
            b"proof": b"live",
            b"lineage.dataset_id": b"db$t",
            b"lineage.namespace": b"db",
            b"lineage.create_run_id": b"abc",
        },
    )
    _patch_ds(monkeypatch, schema)
    out = dataplane.read_schema_metadata(cast(Any, None), cast(Any, {}), ["db", "t"])
    assert out == {"proof": "live"}  # only the user key survives


def test_empty_when_no_schema_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_ds(monkeypatch, pa.schema([pa.field("id", pa.int64())]))  # metadata is None
    assert dataplane.read_schema_metadata(cast(Any, None), cast(Any, {}), ["db", "t"]) == {}
