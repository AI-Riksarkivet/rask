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
from typing import Any, cast

import pytest
from catalog.core.config import Settings
from catalog.services import warehouses
from lance_namespace import TableNotFoundError


def _settings(tmp_path: Any, *, fga_enabled: bool = False) -> Settings:
    data: dict[str, object] = {
        "warehouses_enabled": True,
        # The Settings model refuses FGA without OIDC ("authz needs a user") — a real guard, so the
        # FGA-on case carries a matching issuer rather than being waved through.
        "fga_enabled": fga_enabled,
        **({"oidc_enabled": True, "oidc_issuer": "https://issuer.test", "oidc_audience": "test"} if fga_enabled else {}),
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


def _estate(settings: Settings) -> Any:
    from catalog.api.v1.endpoints import warehouses as wh_ep

    return asyncio.run(wh_ep.list_estate_bindings(settings=settings, token=None, client=None))


def test_the_estate_read_returns_every_binding_in_one_pass(tmp_path: Any) -> None:
    """#86: the per-warehouse read answers for one id, and a page listing the estate was calling it
    once per warehouse — each call re-reading EVERY binding before filtering in Python. This is the
    union `read_bindings` already produces in a single pass.

    The exact-dict assertion also pins the SHAPE: a mapping, not a list, because every caller wants
    to know WHICH warehouse a namespace lives in and a list would make each of them re-derive it."""
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="a-wh", bindings=("a-one", "a-two"))
    _seed(settings, warehouse_id="b-wh", bindings=("b-one",))

    assert _estate(settings).bindings == {"a-one": "a-wh", "a-two": "a-wh", "b-one": "b-wh"}


def test_an_estate_with_no_bindings_is_an_empty_mapping(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="bare-wh")
    assert _estate(settings).bindings == {}


def test_the_estate_read_is_FILTERED_to_what_the_caller_may_see(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """The security half, which the endpoint's own docstring calls load-bearing: a binding names a
    namespace AND the tenant bucket it lives in, so leaking one leaks another tenant's shape. Ran
    untested until the audit — the three tests above all had FGA off."""
    from catalog.api.v1.endpoints import warehouses as wh_ep

    from service_kit.governed import fga as fga_module
    from service_kit.governed.oidc import IDToken

    settings = _settings(tmp_path, fga_enabled=True)
    _seed(settings, warehouse_id="mine-wh", bindings=("mine",))
    _seed(settings, warehouse_id="theirs-wh", bindings=("theirs",))

    async def _only_mine(*args: Any, **kwargs: Any) -> list[str]:
        return ["warehouse:mine-wh"]

    monkeypatch.setattr(fga_module, "list_objects", _only_mine)
    result = asyncio.run(
        wh_ep.list_estate_bindings(settings=settings, token=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1), client=cast(Any, object()))
    )
    assert result.bindings == {"mine": "mine-wh"}, "another tenant's namespace leaked through the filter"


def test_an_unreadable_binding_refuses_rather_than_reporting_a_partial_estate(tmp_path: Any) -> None:
    """Fail-CLOSED, matching the per-warehouse sibling. Answering with the tolerant reader would put
    an invisible bound namespace back — a 200 with one silently missing, which the page's
    read-failed channel cannot see because nothing failed."""
    from lance_namespace import ServiceUnavailableError

    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="ok-wh", bindings=("fine",))
    # A binding object the reader cannot parse — the corrupt-record case, written straight to the store.
    fs, base = __import__("service_kit.lakehouse.objectfs", fromlist=["x"]).fs_and_base(settings.registry_root, settings.storage_options())
    with fs.open_output_stream(f"{base}/_warehouses/bindings/broken.json") as stream:
        stream.write(b"{not json")

    with pytest.raises(ServiceUnavailableError, match="could not be read"):
        _estate(settings)
