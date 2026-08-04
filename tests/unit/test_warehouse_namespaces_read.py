"""``GET /v1/warehouses/{id}/namespaces`` — the bindings READ (#66).

This endpoint exists so no surface has to INFER what a warehouse holds: the lakehouse warehouse
page used to derive its namespace list by grouping the TABLE registry, which made an empty
namespace structurally invisible — the page said "no namespaces" while the delete door refused
409 naming one (observed live 2026-08-04, project ``browserdemo``). The read must therefore
answer from the SAME source the delete door consults: the registry's bindings.

Registry round-trips against a LOCAL filesystem control root, same as the delete-door suite —
no object storage, no mocks where the real primitive runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from catalog.core.config import Settings
from catalog.services import warehouses
from lance_namespace import TableNotFoundError


def _settings(tmp_path: Any) -> Settings:
    data: dict[str, object] = {
        "warehouses_enabled": True,
        "control_root": f"file://{tmp_path}",
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
        "s3_endpoint_url": "http://localhost:9",
    }
    return Settings.model_validate(data)


def _seed(settings: Settings, *, warehouse_id: str, bindings: tuple[str, ...] = ()) -> None:
    so = settings.storage_options()
    record = {
        "id": warehouse_id,
        "bucket": warehouse_id,
        "root_uri": f"s3://{warehouse_id}",
        "project": "acme",
        "status": "active",
        "created_at": "t",
    }
    warehouses.put_warehouse(settings.registry_root, so, record)
    for top_ns in bindings:
        warehouses.bind_namespace(settings.registry_root, so, top_ns, warehouse_id, record["root_uri"])


def _read(settings: Settings, warehouse_id: str) -> Any:
    from catalog.api.v1.endpoints import warehouses as wh_ep

    return asyncio.run(wh_ep.list_warehouse_namespaces(warehouse_id=warehouse_id, settings=settings, token=None, client=None))


def test_bound_namespaces_are_returned_from_the_registry(tmp_path: Any) -> None:
    """Two bindings on this warehouse, one on a sibling — the answer is exactly this warehouse's."""
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="acme-wh", bindings=("acme-bronze", "acme-silver"))
    _seed(settings, warehouse_id="other-wh", bindings=("other-ns",))

    result = _read(settings, "acme-wh")

    assert sorted(result.namespaces) == ["acme-bronze", "acme-silver"]


def test_an_empty_namespace_is_still_listed(tmp_path: Any) -> None:
    """THE #66 case. A binding whose namespace holds zero tables is invisible to any table-derived
    view — but it is precisely what the delete door will refuse on, so the read must show it."""
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="acme-wh", bindings=("empty-ns",))

    assert _read(settings, "acme-wh").namespaces == ["empty-ns"]


def test_a_warehouse_with_no_bindings_answers_an_empty_list(tmp_path: Any) -> None:
    """Empty means EMPTY — a truthful [] the page can render as "safe to delete", distinct from 404."""
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="bare-wh")

    assert _read(settings, "bare-wh").namespaces == []


def test_a_missing_warehouse_is_the_same_404_as_everywhere_else(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    with pytest.raises(TableNotFoundError):
        _read(settings, "nope-wh")
