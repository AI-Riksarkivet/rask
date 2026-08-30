"""Unpinned ingest resolves the estate's store; it never provisions one.

WHY THIS EXISTS. `auth.fgaStoreId: ""` is the chart's default and it is correct — a store id is a
per-cluster ULID, so it cannot be a committed chart default, and every consumer resolves the
`lance-catalog` store by name at boot. Catalog, lineage and medallion do. Ingest did not: it refused
to provision AND refused to look up, so it was the only service in the estate that could not boot on
the estate's own default posture. Every user-bearer request 503'd, reaching a person as
`{"message":"Internal Error"}` from an ETL submit, with a startup warning as the only trace.

The stopgap was `kubectl set env RASK_FGA_STORE_ID=…` against the live deployment — which drifts from
the chart and dies at the next `make k3s-up`.

THE PRINCIPLE THAT SURVIVED. A data writer that mints a store or writes an authorization model becomes
the source of truth for everyone else's permissions. That is still refused. What changed is the
recognition that READING which store exists is not AUTHORING one — so these tests pin both halves:
the resolve must work, and it must never write.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest


class _Store:
    def __init__(self, sid: str, name: str, created_at: int) -> None:
        self.id, self.name, self.created_at = sid, name, created_at


class _Model:
    def __init__(self, mid: str) -> None:
        self.id = mid


class _FakeClient:
    """An OpenFGA client that RECORDS every call, so a write can be asserted absent.

    A mock that only stubs the reads would let a write slip through unnoticed — which is exactly the
    property under test.
    """

    # Class-level ON PURPOSE — `resolve` constructs its own clients, so the recorder cannot be an
    # instance attribute the test holds. Reset per-test by the fixture.
    calls: ClassVar[list[str]] = []
    stores: ClassVar[list[Any]] = []
    models: ClassVar[list[Any]] = []

    def __init__(self, config: Any) -> None:
        self._config = config

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def list_stores(self) -> Any:
        _FakeClient.calls.append("list_stores")
        return type("R", (), {"stores": _FakeClient.stores})()

    async def read_authorization_models(self) -> Any:
        _FakeClient.calls.append("read_authorization_models")
        return type("R", (), {"authorization_models": _FakeClient.models})()

    async def create_store(self, *_: object, **__: object) -> Any:
        _FakeClient.calls.append("create_store")
        raise AssertionError("resolve() created a store — it must never provision")

    async def write_authorization_model(self, *_: object, **__: object) -> Any:
        _FakeClient.calls.append("write_authorization_model")
        raise AssertionError("resolve() wrote an authorization model — a data writer must not author the estate's permissions")


@pytest.fixture
def fake_fga(monkeypatch):
    from service_kit.governed import fga

    _FakeClient.calls = []
    _FakeClient.stores = []
    _FakeClient.models = []
    monkeypatch.setattr(fga, "OpenFgaClient", _FakeClient)
    monkeypatch.setattr(fga, "ClientConfiguration", lambda **kw: kw)
    return _FakeClient


@pytest.mark.anyio
async def test_it_resolves_the_existing_store_and_its_current_model(fake_fga) -> None:
    """The regression: unpinned must find the store the rest of the estate already uses."""
    from service_kit.governed import fga

    fake_fga.stores = [_Store("01STORE", "lance-catalog", 1)]
    fake_fga.models = [_Model("01MODEL"), _Model("01OLDER")]

    assert await fga.resolve("http://fga:8080") == ("01STORE", "01MODEL"), "the CURRENT model is the newest — read_authorization_models answers newest-first"


@pytest.mark.anyio
async def test_it_NEVER_provisions(fake_fga) -> None:
    """The principle that survived. `_FakeClient` raises on either write, so this fails loudly rather
    than silently drifting into provision-on-boot."""
    from service_kit.governed import fga

    fake_fga.stores = [_Store("01STORE", "lance-catalog", 1)]
    fake_fga.models = [_Model("01MODEL")]

    await fga.resolve("http://fga:8080")

    assert "create_store" not in fake_fga.calls
    assert "write_authorization_model" not in fake_fga.calls


@pytest.mark.anyio
async def test_no_store_means_None_not_a_new_one(fake_fga) -> None:
    """An absent store means the estate has not been bootstrapped. The caller must fail closed —
    creating one here would hand a data writer the estate's permissions."""
    from service_kit.governed import fga

    fake_fga.stores = []

    assert await fga.resolve("http://fga:8080") is None
    assert "create_store" not in fake_fga.calls


@pytest.mark.anyio
async def test_a_store_with_no_model_is_also_None(fake_fga) -> None:
    """Half-provisioned is not usable, and writing the missing model is precisely the authoring this
    plane refuses. A client built against a store with no model would 500 on every check instead of
    503-ing honestly."""
    from service_kit.governed import fga

    fake_fga.stores = [_Store("01STORE", "lance-catalog", 1)]
    fake_fga.models = []

    assert await fga.resolve("http://fga:8080") is None
    assert "write_authorization_model" not in fake_fga.calls


@pytest.mark.anyio
async def test_it_takes_the_NEWEST_store_when_names_collide(fake_fga) -> None:
    """Two stores answered to one name on the live estate — a provision-by-name race across services —
    and the resulting split sent a whole afternoon chasing a 403 that was really 'wrong backend'. The
    tie-break must match `provision`'s, or a resolver and a provisioner land on different stores."""
    from service_kit.governed import fga

    fake_fga.stores = [_Store("01OLD", "lance-catalog", 1), _Store("01NEW", "lance-catalog", 9)]
    fake_fga.models = [_Model("01MODEL")]

    resolved = await fga.resolve("http://fga:8080")
    assert resolved is not None and resolved[0] == "01NEW"


@pytest.mark.anyio
async def test_it_ignores_stores_with_a_DIFFERENT_name(fake_fga) -> None:
    """Resolution is by name; an unrelated store must not be adopted."""
    from service_kit.governed import fga

    fake_fga.stores = [_Store("01OTHER", "something-else", 5)]

    assert await fga.resolve("http://fga:8080") is None
