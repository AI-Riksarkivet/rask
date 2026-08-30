"""S4 — `create` carries the reproducibility pin and caller run facets (OPEN-WORK.md §8 S4).

The asymmetry this closes exists independent of the annotation plane: `merge_insert` accepts a
version-pinned `source` and an `X-Lance-Run-Facets` header; `create` accepted neither — so EVERY
first write of a derived table (a Ray job's output, an export, an annotation publish) was emitted
with no reproducibility pin and no producer facet. These tests drive the `create_table` handler
directly with structural fakes (no catalog app, no store), the same seam the S4 spec names.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import InvalidInputError

from catalog.api.v1.endpoints import data as data_ep
from catalog.services import table_create


def _ipc(table: pa.Table) -> bytes:
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return bytes(sink.getvalue())


class _Settings:
    delimiter = "$"
    fga_enabled = False
    lineage_emit_enabled = False  # skips the in-body metadata inject; not what these tests pin
    multibase_data_base_list: list[str] = []
    allow_external_blobs = False
    external_blob_base_list: list[str] = []


class _Token:
    sub = "svc-annotator"


class _Emitter:
    """Captures what the handler emits — the assertion surface."""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []

    async def emit_create(self, **kwargs: Any) -> None:
        self.creates.append(kwargs)

    async def emit_write(self, **kwargs: Any) -> None:  # pragma: no cover - protocol completeness
        raise AssertionError("create must emit via emit_create")


class _Response:
    version = 1
    location = "s3://lance-catalog/silver/t.lance"


async def _create(monkeypatch: pytest.MonkeyPatch, emitter: _Emitter, **kwargs: Any) -> Any:
    """Drive the real handler with the write path faked out — dataplane commits are not under test."""
    monkeypatch.setattr(table_create.dataplane, "create_table", lambda *a, **k: _Response())
    monkeypatch.setattr(table_create.dataplane, "payload_schema_fields", lambda *a, **k: [])

    async def _no_control(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(table_create, "emit_control", _no_control)
    body = _ipc(pa.table({"x": [1]}))
    return await data_ep.create_table(
        "silver$labels_1",
        ns=cast(Any, object()),
        settings=cast(Any, _Settings()),
        token=cast(Any, _Token()),
        client=None,
        emitter=cast(Any, emitter),
        control=cast(Any, None),
        so={},
        data=body,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_create_carries_the_pin_and_the_facet_onto_the_emitted_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin surfaces as a version-pinned INPUT and the facet survives under its own name —
    the exact provenance §7.2 promises the publish, now with a transport."""
    emitter = _Emitter()
    facets = {"annotationProject": {"projectId": "p1", "taskCount": 3}}

    await _create(
        monkeypatch,
        emitter,
        source="bronze$pages",
        source_version=24,
        run_facets_json=json.dumps(facets),
    )

    emitted = emitter.creates[0]
    assert emitted["inputs"] is not None, "create emitted no INPUT for a version-pinned source"
    (ref,) = emitted["inputs"]
    assert (ref.namespace, ref.name, ref.version) == ("bronze", "bronze$pages", 24)
    shaped = emitted["extra_run_facets"]
    assert shaped is not None and "annotationProject" in shaped
    assert shaped["annotationProject"]["projectId"] == "p1"
    assert shaped["annotationProject"]["_producer"], "the facet lost its spec stamp"


@pytest.mark.asyncio
async def test_a_plain_create_emits_exactly_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pin, no facet — the default path must be byte-for-byte the pre-S4 emit."""
    emitter = _Emitter()

    await _create(monkeypatch, emitter)

    emitted = emitter.creates[0]
    assert emitted.get("inputs") is None
    assert emitted.get("extra_run_facets") is None


@pytest.mark.asyncio
async def test_source_version_without_source_is_a_400_before_any_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_merge_source_pin`'s existing rule, now reachable from create: a version with nothing to pin
    is refused up front — never a committed create whose provenance silently dropped."""
    emitter = _Emitter()
    writes: list[Any] = []
    monkeypatch.setattr(table_create.dataplane, "create_table", lambda *a, **k: writes.append(a) or _Response())

    with pytest.raises(InvalidInputError, match="source_version requires source"):
        await _create(monkeypatch, emitter, source_version=3)

    assert writes == [], "the table was created before the pin was validated"
    assert emitter.creates == []


@pytest.mark.asyncio
async def test_a_reserved_facet_name_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lance`, `author`, `errorMessage`, `progress`, `parent` are the catalog's own. A producer
    smuggling one in would overwrite trusted provenance."""
    emitter = _Emitter()

    with pytest.raises(InvalidInputError, match="reserved"):
        await _create(monkeypatch, emitter, run_facets_json=json.dumps({"author": {"x": 1}}))

    assert emitter.creates == []
