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
    monkeypatch.setattr(dataplane, "open_dataset_unchecked", lambda *_a, **_k: SimpleNamespace(schema=schema))


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
