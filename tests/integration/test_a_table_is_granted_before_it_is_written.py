"""[[LH-588]], [[LH-585]] A table's grants, and a namespace's, are written before it, so neither stands without them.

Every ``table`` relation resolves through a direct tuple or ``from parent``, so a table holding no tuple denies every
principal, its creator included. Create, declare and register therefore write the table's grants (its owner, and its
``parent`` and ``child`` edges, keyed by the uuid the door mints) before the backend write that records the table, and a
process killed between the two leaves either the table with its grants or the grants alone. Grants alone are a ghost's,
and the drift repair revokes them once nothing can still record a table under their uuid: no location claim does, or the
one that does has outlived its lease with no table standing at it.

ONE ESTATE ON MOTO, TWO SERVICES (`catalog_and_maintenance`): the real catalog app as alice, deciding on a real OpenFGA
store of the test's own that holds what its doors write, and the real maintenance audit with drift repair armed, asking
the catalog's own grant doors for the census and the revokes. A kill is a
``BaseException`` raised where the process stops, so no ``except Exception`` compensation in the door runs after it, as none
runs after a SIGKILL: at the door's grant (its OpenFGA write) or at its backend write (the ``register_table`` call that
records the table's row). The app's middleware answers it 500, which no caller reads. A door that fails once the table
stands, and whose undo of the table fails too, keeps the table's grants with it.

A namespace is granted the same way ([[LH-585]]): every ``namespace`` relation resolves through a direct tuple or ``from
parent`` too, so one standing with no tuple is invisible to its creator and its project's admin alike, and its name
refuses every retry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError

from catalog.core import native
from catalog.core.config import get_settings
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed import fga_writes
from lakehouse.records import location_claims
from tests.governed_catalog import StoreTuples
from tests.integration.catalog_and_maintenance import audited, catalog_and_maintenance
from tests.integration.maintenance_vend_doors import CATALOG_URL, catalog_doors


PROJECT = "acme"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class Killed(BaseException):
    """The process ends here: nothing after it runs, no handler of ``Exception`` included."""


class _Estate:
    def __init__(self, catalog: TestClient, maintenance: TestClient, store: StoreTuples, namespace: str) -> None:
        self.catalog = catalog
        self.maintenance = maintenance
        self.store = store
        self.namespace = namespace

    def tables_granted(self) -> set[str]:
        """Every ``table:`` object some tuple names, as its user or as its object."""
        return {name for held in self.store.tuples for name in (held[0], held[2]) if name.startswith("table:")}

    def standing(self, table: str) -> str | None:
        """The object ``table`` is governed as while it stands, else ``None``."""
        answer = self.catalog.post(f"/v1/table/{table}/describe", json={})
        return f"table:{answer.json()['properties']['rask.uuid']}" if answer.status_code == 200 else None


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch, openfga_store: Any, dex_issuer: Any) -> Iterator[_Estate]:  # noqa: ANN401 — the root conftest's fixtures
    """Namespace ``bronze`` in project ``acme``'s warehouse, made by alice, and maintenance with drift repair armed."""
    env = {"MAINTENANCE_DRIFT_REPAIR_ENABLED": "true", "MAINTENANCE_DRIFT_REPAIR_DRY_RUN": "false", "MAINTENANCE_CATALOG_URL": CATALOG_URL}
    with catalog_and_maintenance(moto_url, monkeypatch, env=env, openfga_store=openfga_store, dex_issuer=dex_issuer) as both:
        warehouse = f"lh588-{uuid.uuid4().hex[:10]}-wh"
        for path, body in (("/management/v1/projects", {"id": PROJECT}), ("/management/v1/warehouses", {"id": warehouse, "project": PROJECT})):
            answer = both.catalog.post(path, json=body)
            assert answer.status_code == 200, f"{path}: {answer.text}"
        namespace = f"{warehouse}$bronze"
        assert both.catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        assert isinstance(both.store, StoreTuples), "the estate decides on a real OpenFGA store"
        yield _Estate(both.catalog, both.maintenance, both.store, namespace)


def _create(estate: _Estate, table: str) -> None:
    estate.catalog.post(f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})


def _declare(estate: _Estate, table: str) -> None:
    estate.catalog.post(f"/v1/table/{table}/declare", json={})


def _register(estate: _Estate, table: str) -> None:
    """Register bytes a pipeline wrote outside the catalog's doors, beside the tables the catalog makes."""
    warehouse = table.split("$", 1)[0]
    root = estate.catalog.get(f"/management/v1/warehouses/{warehouse}").json()["root_uri"]
    relative = f"outside-{uuid.uuid4().hex[:8]}"
    options = get_settings().storage_options().in_clear()
    lance.write_dataset(_ROWS, f"{root.rstrip('/')}/{relative}", storage_options=options, data_storage_version="2.2", enable_stable_row_ids=True)
    estate.catalog.post(f"/v1/table/{table}/register", json={"location": relative})


def _at_the_grant(monkeypatch: pytest.MonkeyPatch, killed: list[str]) -> None:
    async def _killed(*_args: object, **_kwargs: object) -> None:
        killed.append("grant")
        raise Killed

    monkeypatch.setattr(fga_writes, "grant_on_create", _killed)


def _at_the_backend_write(monkeypatch: pytest.MonkeyPatch, killed: list[str]) -> None:
    call = native.call

    def _killed(ns: Any, method: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the backend's own request and answer
        if method == "register_table":
            killed.append(method)
            raise Killed
        return call(ns, method, *args, **kwargs)

    monkeypatch.setattr(native, "call", _killed)


def _a_vend_and_its_undo_failing(monkeypatch: pytest.MonkeyPatch, killed: list[str]) -> None:
    """Not a kill: the declaration's credential fails after the backend recorded it, and so does the drop that would undo
    it, so the declared table stands and its door answers the vend's failure."""
    from catalog.api.v1.endpoints import tables

    call = native.call

    async def _no_credential(*_args: object, **_kwargs: object) -> None:
        killed.append("vend")
        raise ServiceUnavailableError("the store's STS did not answer")

    def _no_drop(ns: Any, method: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the backend's own request and answer
        if method == "drop_table":
            raise ServiceUnavailableError("the store did not answer the drop")
        return call(ns, method, *args, **kwargs)

    monkeypatch.setattr(tables, "_vend_on_spec_door", _no_credential)
    monkeypatch.setattr(native, "call", _no_drop)


@pytest.mark.parametrize(
    ("door", "kill"),
    [
        *(
            pytest.param(door, kill, id=f"{door.__name__.strip('_')}-{kill.__name__.strip('_').replace('_', '-')}")
            for door in (_create, _declare, _register)
            for kill in (_at_the_grant, _at_the_backend_write)
        ),
        pytest.param(_declare, _a_vend_and_its_undo_failing, id="declare-whose-vend-and-undo-fail"),
    ],
)
def test_a_door_killed_between_its_two_writes_leaves_no_table_without_its_grants(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, door: Callable[[_Estate, str], None], kill: Callable[[pytest.MonkeyPatch, list[str]], None]
) -> None:
    table = f"{estate.namespace}$t"
    before = estate.tables_granted()
    killed: list[str] = []
    with pytest.MonkeyPatch.context() as killing:
        kill(killing, killed)
        door(estate, table)
    assert len(killed) == 1, killed
    standing = estate.standing(table)
    # Its owner, its parent edge and its namespace's child edge naming it.
    assert standing is None or len(estate.store.naming([standing]).get(standing, set())) == 3, f"{table} stands holding {estate.store.naming([standing])}"

    # Every claim the kill left has outlived its lease.
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))
    with catalog_doors(estate.catalog):
        audited(estate.maintenance)

    assert estate.tables_granted() - before == ({standing} if standing else set())


def _at_the_namespace_grant(monkeypatch: pytest.MonkeyPatch, killed: list[str]) -> None:
    async def _killed(*_args: object, **_kwargs: object) -> None:
        killed.append("grant")
        raise Killed

    monkeypatch.setattr(fga_writes, "grant_before_arrival", _killed)


def _at_the_namespace_write(monkeypatch: pytest.MonkeyPatch, killed: list[str]) -> None:
    call = native.call

    def _killed(ns: Any, method: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the backend's own request and answer
        if method == "create_namespace":
            killed.append(method)
            raise Killed
        return call(ns, method, *args, **kwargs)

    monkeypatch.setattr(native, "call", _killed)


@pytest.mark.parametrize("kill", [pytest.param(_at_the_namespace_grant, id="at-the-grant"), pytest.param(_at_the_namespace_write, id="at-the-backend-write")])
def test_a_namespace_create_killed_between_its_two_writes_leaves_no_namespace_without_its_grants(
    estate: _Estate, kill: Callable[[pytest.MonkeyPatch, list[str]], None]
) -> None:
    """Killed at its grant, the door has written nothing. Killed at its backend write, it leaves the owner and the
    ``parent`` edge of a uuid no namespace holds, and no ``child`` edge: nothing that names another object's side, so
    nothing anyone holds a relation through."""
    namespace = f"{estate.namespace}$silver"
    before = set(estate.store.tuples)
    killed: list[str] = []
    with pytest.MonkeyPatch.context() as killing:
        kill(killing, killed)
        estate.catalog.post(f"/v1/namespace/{namespace}/create", json={})
    assert len(killed) == 1, killed

    left = set(estate.store.tuples) - before

    assert [held for held in left if held[1] == "child"] == [], f"the dead create names a child of a standing object: {left}"
    created = estate.catalog.post(f"/v1/namespace/{namespace}/create", json={})
    assert created.status_code == 200, f"{namespace} stands and no tuple names it, or its name is not free: {created.text}"
    standing = f"namespace:{created.json()['properties']['rask.uuid']}"
    assert left.isdisjoint(estate.store.naming([standing]).get(standing, set())), "the namespace created since holds the dead create's grants"
    # Its owner, its parent edge and its parent's child edge naming it.
    assert len(estate.store.naming([standing]).get(standing, set())) == 3, estate.store.naming([standing])


def test_a_namespace_whose_create_answer_was_lost_keeps_its_grants(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """The backend records the namespace and its answer never arrives: the door fails, and the namespace it cannot
    disown keeps its owner and its ``parent`` edge, so its creator and its project's admin reach it."""
    namespace = f"{estate.namespace}$silver"
    call = native.call

    def _answer_lost(ns: Any, method: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the backend's own request and answer
        answer = call(ns, method, *args, **kwargs)
        if method == "create_namespace":
            raise ServiceUnavailableError("the store's answer was lost")
        return answer

    with pytest.MonkeyPatch.context() as losing:
        losing.setattr(native, "call", _answer_lost)
        failed = estate.catalog.post(f"/v1/namespace/{namespace}/create", json={})
    assert failed.status_code == 503, failed.text

    described = estate.catalog.post(f"/v1/namespace/{namespace}/describe", json={})

    assert described.status_code == 200, described.text
    standing = f"namespace:{described.json()['properties']['rask.uuid']}"
    assert {held[1] for held in estate.store.naming([standing]).get(standing, set())} == {"owner", "parent"}
