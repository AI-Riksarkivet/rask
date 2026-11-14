"""[[LH-588]] A table's grants are written before the table, so no table stands without them.

Every ``table`` relation resolves through a direct tuple or ``from parent``, so a table holding no tuple denies every
principal, its creator included. Create, declare and register therefore write the table's grants (its owner, and its
``parent`` and ``child`` edges, keyed by the uuid the door mints) before the backend write that records the table, and a
process killed between the two leaves either the table with its grants or the grants alone. Grants alone are a ghost's,
and the drift repair revokes them once nothing can still record a table under their uuid: no location claim does, or the
one that does has outlived its lease with no table standing at it.

ONE ESTATE ON MOTO, TWO SERVICES (`catalog_and_maintenance`): the real catalog app as alice, and the real maintenance
audit with drift repair armed, asking the catalog's own grant doors for the census and the revokes. A kill is a
``BaseException`` raised where the process stops, so no ``except Exception`` compensation in the door runs after it, as none
runs after a SIGKILL: at the door's grant (its OpenFGA write) or at its backend write (the ``register_table`` call that
records the table's row). The app's middleware answers it 500, which no caller reads. A door that fails once the table
stands, and whose undo of the table fails too, keeps the table's grants with it.
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

from catalog.core.config import get_settings
from catalog.services import native
from lakehouse import location_claims
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.governed import fga
from tests.integration.catalog_and_maintenance import audited, catalog_and_maintenance
from tests.integration.maintenance_vend_doors import CATALOG_URL, catalog_doors
from tests.integration.openfga_grants import TupleStore


PROJECT = "acme"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class Killed(BaseException):
    """The process ends here: nothing after it runs, no handler of ``Exception`` included."""


class _Estate:
    def __init__(self, catalog: TestClient, maintenance: TestClient, store: TupleStore, namespace: str) -> None:
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
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    """Namespace ``bronze`` in project ``acme``'s warehouse, made by alice, and maintenance with drift repair armed."""
    env = {"MAINTENANCE_DRIFT_REPAIR_ENABLED": "true", "MAINTENANCE_DRIFT_REPAIR_DRY_RUN": "false", "MAINTENANCE_CATALOG_URL": CATALOG_URL}
    with catalog_and_maintenance(moto_url, monkeypatch, env=env) as both:
        warehouse = f"lh588-{uuid.uuid4().hex[:10]}-wh"
        for path, body in (("/management/v1/projects", {"id": PROJECT}), ("/management/v1/warehouses", {"id": warehouse, "project": PROJECT})):
            answer = both.catalog.post(path, json=body)
            assert answer.status_code == 200, f"{path}: {answer.text}"
        namespace = f"{warehouse}$bronze"
        assert both.catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
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

    monkeypatch.setattr(fga, "grant_on_create", _killed)


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
