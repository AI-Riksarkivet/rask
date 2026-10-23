"""Cross-axis identity (P0 #4) — infra-free.

The lakehouse names a table by ONE id string on two axes: the **lineage Dataset** name and the **id
embedded in the Lance file** (#21). They MUST be byte-identical, else a lineage node names one table while the
file names another and provenance splits. The real ``create_table`` handler derives the lineage emit
``table_id``/``namespace`` and the embedded Lance metadata from one canonicalisation of the identifier, and stamps
the file with the run id the event carries. The OpenFGA object is keyed by the table's uuid, not its name
(ADR 0173 §8).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pyarrow as pa
import pytest

from service_kit.control_emit import NoopControlEmitter
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal
from service_kit.lancekit.arrow_ipc import encode_arrow_stream
from tests.unit.named_resolver import NamedResolver
from tests.unit.no_uuids import NO_OBJECT_UUIDS


def _settings() -> Any:
    return SimpleNamespace(
        lineage_emit_enabled=True,
        allow_external_blobs=False,
        external_blob_base_list=[],
        # [[LH-067]] Read by the create path to compose per-base credentials; empty renders today's map.
        multibase_base_credential_ref_map={},
        dapr_secret_store="",
        dapr_secret_s3_field="",
        # [[XC-104]] The door holds the body to this once its compressed buffers inflate.
        max_body_bytes=256 * 1024 * 1024,
        # [[LH-279]] Where the create records the bases it registers; the write is faked here.
        registry_root="",
        # [[LH-209]] What the create judges a requested external blob base against; this create requests none.
        root="",
        models_root="",
        model_artifacts_root="",
        reserved_bucket_set=frozenset(),
        storage_options=dict,
    )


class _RecordingEmitter:
    """Captures the create-emit kwargs (the lineage axis)."""

    def __init__(self) -> None:
        self.create: dict[str, Any] | None = None

    async def emit_create(self, **kwargs: Any) -> None:
        self.create = kwargs


def test_create_handler_keeps_both_name_axes_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real ``create_table`` handler and assert the lineage Dataset name and the embedded Lance metadata
    agree byte for byte, and that the file carries the run id the event does.

    Guards the regression where one axis is derived differently from the other: the captures below would diverge.
    """
    from catalog.api.v1.endpoints import data
    from catalog.services import table_create

    stamped: dict[str, Any] = {}
    written: dict[str, pa.Table] = {}

    # `**_kw` on purpose: this fake stands in for `seed_ownership`, whose grants are keyed by the uuid, not by
    # either name axis this test compares — pinning its signature here only makes an unrelated change to the
    # seam look like an identity regression.
    async def _seed(*_a: object, **_kw: object) -> None:
        return None

    real_build_meta = table_create.build_lineage_metadata

    def _build_meta(*, namespace: str, run_id: str) -> dict[str, str]:
        stamped["namespace"] = namespace
        stamped["run_id"] = run_id
        return real_build_meta(namespace=namespace, run_id=run_id)

    def _facade_create(_ns: object, _so: object, _segments: list[str], table: pa.Table, **_kw: object) -> Any:
        # Stubs the write, not the table handed to it: this test is about the THREE-AXIS identity the
        # endpoint derives, and the Lance metadata axis is only real if it reaches the table written.
        written["table"] = table
        return SimpleNamespace(version=1, location="s3://lakehouse/0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")

    monkeypatch.setattr(table_create.fga_deps, "seed_ownership", _seed)
    # The write is faked, so no claim records its uuid; the uuid the seed is handed is not an axis compared here.
    monkeypatch.setattr(table_create.table_claims, "uuid_at", lambda *_a: "0b6f8a52-4c1e-4f7a-9d3e-2a1c5b7e9f01")
    monkeypatch.setattr(table_create, "build_lineage_metadata", _build_meta)
    monkeypatch.setattr(table_create.dataplane, "create_table", _facade_create)

    emitter = _RecordingEmitter()
    asyncio.run(
        data.create_table(
            id="alpha$bronze$images",
            ns=cast(Any, object()),
            root="s3://root",
            settings=_settings(),
            token=PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)),
            client=cast(Any, object()),
            emitter=cast(Any, emitter),
            control=cast(Any, NoopControlEmitter()),
            so=cast(Any, {}),  # unused on the native (non-blob) path this test drives
            data=encode_arrow_stream(pa.table({"id": [1]})),
            mode=None,
            properties=None,
            authorization="Bearer tok",
            run=None,
            uuids=NO_OBJECT_UUIDS,
            resolver=NamedResolver(),
        )
    )

    canonical = "alpha$bronze$images"
    # Lineage axis: the emitted Dataset name + parent namespace.
    assert emitter.create is not None
    assert emitter.create["table_id"] == canonical
    assert emitter.create["namespace"] == "alpha$bronze"
    assert emitter.create["author"] == "dex~alice"
    # Data axis: the namespace stamped into the Lance file's schema metadata (#21).
    assert stamped["namespace"] == "alpha$bronze"
    # #21: the run id stamped into the Lance file is the SAME one the lineage event carries.
    assert stamped["run_id"] == emitter.create["run_id"]
    assert emitter.create["run_id"]  # and it's non-empty
    # ...and both coordinates are on the table handed to the write, not only computed. `lineage.dataset_id` is the
    # table's uuid, which the write stamps once it has claimed the location (`dataplane.create_table`).
    metadata = written["table"].schema.metadata or {}
    assert metadata.get(b"lineage.namespace") == b"alpha$bronze"
    assert metadata.get(b"lineage.create_run_id") == emitter.create["run_id"].encode()
    # The WROTE edge's schema facet is read off the table written.
    assert [field["name"] for field in emitter.create["schema_fields"]] == ["id"]
