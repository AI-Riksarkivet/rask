"""Cross-axis identity under a non-default delimiter (P0 #4) — infra-free.

The lakehouse keys a table by ONE id string across three governance axes: the **OpenFGA object**
(``table:<id>``), the **lineage Dataset** name, and the **id embedded in the Lance file** (#21). They
MUST be byte-identical, else a grant lands on ``table:a$b`` while a check / lineage node uses ``table:a.b``
and silently denies / splits provenance. The delimiter is configurable (``LANCE_NS_DELIMITER``), so this
identity has to hold under a *non-default* delimiter too — the audit ``w8u4rc2tg`` flagged this as latent.

Two layers:

* the **primitive invariant** — ``parse_identifier`` ∘ ``canonical_object_id`` round-trips byte-for-byte
  under any delimiter (the single source of truth both the grant and the emit derive from), and
* the **handler wiring** — the real ``create_table`` handler derives the FGA grant object, the lineage
  emit ``table_id``/``namespace``, and the embedded Lance metadata id from that one canonicalisation, so
  all three agree under a ``.``-delimited deployment.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.core.control_emit import NoopControlEmitter
from catalog.core.identifiers import parse_identifier

from service_kit.governed import fga


# Default plus three non-default delimiters (single-char and multi-char).
_DELIMITERS = ["$", ".", "/", "::"]


@pytest.mark.parametrize("delimiter", _DELIMITERS)
def test_parse_then_canonicalize_round_trips_byte_for_byte(delimiter: str) -> None:
    # The id a user submits, split into segments and re-joined into the canonical OpenFGA/lineage id, must
    # come back identical — this is what keeps the grant path and the check/emit path from drifting apart.
    identifier = delimiter.join(["alpha", "bronze", "images"])
    segments = parse_identifier(identifier, delimiter)
    assert segments == ["alpha", "bronze", "images"]
    assert fga.canonical_object_id(segments, delimiter=delimiter) == identifier


@pytest.mark.parametrize("delimiter", _DELIMITERS)
def test_parent_namespace_is_all_but_last_segment(delimiter: str) -> None:
    segments = parse_identifier(delimiter.join(["alpha", "bronze", "images"]), delimiter)
    assert fga.parent_namespace_id(segments, delimiter=delimiter) == delimiter.join(["alpha", "bronze"])


@pytest.mark.parametrize("delimiter", _DELIMITERS)
def test_root_level_table_has_no_parent_namespace(delimiter: str) -> None:
    # A single-segment (top-level) table has no parent namespace under any delimiter.
    assert fga.parent_namespace_id(parse_identifier("solo", delimiter), delimiter=delimiter) is None


def _settings(delimiter: str) -> Any:
    return SimpleNamespace(
        delimiter=delimiter,
        lineage_emit_enabled=True,
        allow_external_blobs=False,
        external_blob_base_list=[],
    )


class _RecordingEmitter:
    """Captures the create-emit kwargs (the lineage axis)."""

    def __init__(self) -> None:
        self.create: dict[str, Any] | None = None

    async def emit_create(self, **kwargs: Any) -> None:
        self.create = kwargs


def test_create_handler_keeps_all_three_axes_identical_under_non_default_delimiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real ``create_table`` handler under a ``.``-delimited config and assert the FGA grant
    object, the lineage Dataset name, and the embedded Lance metadata id are byte-identical.

    Guards the regression where one axis is derived differently (e.g. a hardcoded ``$``): if the handler
    ever stopped funnelling all three through ``canonical_object_id(segments, settings.delimiter)``, the
    three captures below would diverge.
    """
    from catalog.api.v1.endpoints import data

    granted: dict[str, Any] = {}
    stamped: dict[str, Any] = {}

    # `**_kw` on purpose: this fake stands in for `seed_ownership`, which the compensating seam calls
    # with whatever keywords it grows (it forwards `parent_object` since diff2 F3). This test is about
    # the RESOURCE and SEGMENTS axes agreeing — pinning the rest of the signature here only makes an
    # unrelated change to the seam look like an identity regression.
    async def _seed(_client: object, _settings: object, _token: object, *, resource: str, segments: list[str], **_kw: object) -> None:
        granted["resource"] = resource
        granted["segments"] = segments

    def _build_meta(*, table_id: str, namespace: str, run_id: str) -> dict[str, str]:
        stamped["table_id"] = table_id
        stamped["namespace"] = namespace
        stamped["run_id"] = run_id
        return {"lineage.dataset_id": table_id}

    def _facade_create(*_a: object, **_k: object) -> Any:
        # Stub the whole write facade: this test is about the THREE-AXIS identity the endpoint derives
        # (FGA object id, lineage Dataset name, Lance metadata id), not the write. Every create now routes
        # through the direct 2.2 path (dataplane.create_table -> declare + write_dataset), so patching
        # native.call no longer intercepts it — the facade is the correct seam. Returns the same fake
        # location the old native stub did.
        return SimpleNamespace(version=1, location="s3://lakehouse/uuid_alpha.bronze.images")

    monkeypatch.setattr(data.fga_deps, "seed_ownership", _seed)
    monkeypatch.setattr(data, "build_lineage_metadata", _build_meta)
    monkeypatch.setattr(data, "inject_into_arrow_stream", lambda payload, _meta: payload)  # no real Arrow
    monkeypatch.setattr(data.dataplane, "create_table", _facade_create)

    emitter = _RecordingEmitter()
    asyncio.run(
        data.create_table(
            id="alpha.bronze.images",
            ns=cast(Any, object()),
            settings=_settings("."),
            token=cast(Any, SimpleNamespace(sub="alice")),
            client=cast(Any, object()),
            emitter=cast(Any, emitter),
            control=cast(Any, NoopControlEmitter()),
            so=cast(Any, {}),  # unused on the native (non-blob) path this test drives
            data=b"arrow-ipc-bytes",
            mode=None,
            properties=None,
            authorization="Bearer tok",
        )
    )

    canonical = "alpha.bronze.images"
    # Lineage axis: the emitted Dataset name + parent namespace.
    assert emitter.create is not None
    assert emitter.create["table_id"] == canonical
    assert emitter.create["namespace"] == "alpha.bronze"
    assert emitter.create["author"] == "alice"
    # Data axis: the id stamped into the Lance file's schema metadata (#21).
    assert stamped["table_id"] == canonical
    assert stamped["namespace"] == "alpha.bronze"
    # FGA grant axis: the grant lands on table:<same canonical id> (NOT table:alpha$bronze$images).
    grant_object = f"{granted['resource']}:{fga.canonical_object_id(granted['segments'], delimiter='.')}"
    assert grant_object == f"table:{canonical}"
    # #21: the run id stamped into the Lance file is the SAME one the lineage event carries.
    assert stamped["run_id"] == emitter.create["run_id"]
    assert emitter.create["run_id"]  # and it's non-empty
