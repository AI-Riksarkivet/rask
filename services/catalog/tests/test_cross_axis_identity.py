"""Cross-axis identity (P0 #4) — infra-free.

The lakehouse names a table by ONE id string on two axes: the **lineage Dataset** name and the **id
embedded in the Lance file** (#21). They MUST be byte-identical, else a lineage node names one table while the
file names another and provenance splits. The real ``create_table`` handler derives the lineage emit
``table_id``/``namespace`` and the embedded Lance metadata from one canonicalisation of the identifier. The OpenFGA object is keyed by the table's uuid, not its name
(ADR 0173 §8).
"""

import asyncio
from typing import Any, cast

import pyarrow as pa
import pytest
from lance_namespace import CreateTableResponse

from catalog.api.authz.context import AuthzContext
from catalog.core.config import Settings
from lakehouse.arrow_ipc import encode_arrow_stream
from lakehouse.events.control_emit import NoopControlEmitter
from tests.callers import person
from tests.catalog_settings import catalog_settings
from tests.lineage_emitter_double import LineageEmitterDouble
from tests.unit.named_resolver import NamedResolver
from tests.unit.no_uuids import NO_OBJECT_UUIDS


_DATASETS = "lance-namespace://rask.example.org/api/catalog"


def _settings() -> Settings:
    # The create records no bases (its write is faked) and requests no external blob base; no namespace is the provenance
    # one (ADR 0187); and the Lance file names the table under the catalog's dataset namespace, as every event does
    # ([[LH-553]]). A catalog that emits lineage names the bus it publishes on, which this test never dials.
    return catalog_settings(
        root="",
        lineage_emit_enabled=True,
        bus={"RASK_BUS_URL": "nats://bus.test:4222", "RASK_BUS_USER": "catalog", "RASK_BUS_TOKEN_FILE": "/var/run/unread"},
        provenance_namespace="",
        lineage_dataset_namespace=_DATASETS,
    )


def test_create_handler_keeps_both_name_axes_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real ``create_table`` handler and assert the lineage Dataset name and the embedded Lance metadata
    agree byte for byte.

    Guards the regression where one axis is derived differently from the other: the captures below would diverge.
    """
    from catalog.api import table_create
    from catalog.api.idempotency import KeyedDoor
    from catalog.api.v1.endpoints import data

    stamped: dict[str, Any] = {}
    written: dict[str, pa.Table] = {}

    real_build_meta = table_create.build_lineage_metadata

    def _build_meta(*, namespace: str) -> dict[str, str]:
        stamped["namespace"] = namespace
        return real_build_meta(namespace=namespace)

    def _facade_create(_ns: object, _so: object, _segments: list[str], table: pa.Table, **_kw: object) -> Any:
        # Stubs the write, not the table handed to it: this test is about the THREE-AXIS identity the
        # endpoint derives, and the Lance metadata axis is only real if it reaches the table written.
        written["table"] = table
        return CreateTableResponse(version=1, location="s3://lakehouse/0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")

    # The write is faked, so no claim records its uuid; the uuid the seed is handed is not an axis compared here.
    monkeypatch.setattr(table_create.table_claims, "uuid_at", lambda *_a: "0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")
    monkeypatch.setattr(table_create, "build_lineage_metadata", _build_meta)
    monkeypatch.setattr(table_create.creation, "create_table", _facade_create)

    emitter = LineageEmitterDouble()
    asyncio.run(
        data.create_table(
            context=AuthzContext(
                client=cast(Any, object()),
                settings=_settings(),
                token=person("alice"),
            ),
            id="alpha$bronze$images",
            keyed=KeyedDoor(registry_root="", storage_options={}, scope="anonymous", key=None),
            ns=cast(Any, object()),
            root="s3://root",
            emitter=cast(Any, emitter),
            control=cast(Any, NoopControlEmitter()),
            so=cast(Any, {}),  # unused on the native (non-blob) path this test drives
            data=encode_arrow_stream(pa.table({"id": [1]})),
            mode=None,
            properties=None,
            run=None,
            uuids=NO_OBJECT_UUIDS,
            resolver=NamedResolver(),
        )
    )

    canonical = "alpha$bronze$images"
    # Lineage axis: the emitted Dataset name + parent namespace.
    (create,) = emitter.writes
    assert create["table_id"] == canonical
    assert create["namespace"] == "alpha$bronze"
    assert create["author"] == "dex~alice"
    # Data axis: the dataset namespace stamped into the Lance file's schema metadata (#21), the catalog's own, which the
    # emitter names every dataset under ([[LH-553]]).
    assert stamped["namespace"] == _DATASETS
    # ...and both coordinates are on the table handed to the write, not only computed. `lineage.dataset_id` is the
    # table's uuid, which the write stamps once it has claimed the location (`tables.creation.create_table`).
    metadata = written["table"].schema.metadata or {}
    assert metadata.get(b"lineage.namespace") == _DATASETS.encode()
    # The WROTE edge's schema facet is read off the table written.
    assert [field["name"] for field in create["schema_fields"]] == ["id"]
