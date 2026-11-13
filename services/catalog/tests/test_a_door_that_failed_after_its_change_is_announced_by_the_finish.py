"""A door that changes state files what it will announce before the change, so a door that stops after its change is
still announced, once, by the finish maintenance's hourly audit asks for ([[LH-576]], ADR 0205).

Each door here is stopped at its first send, as a process ending between its change and its announcement would stop
it. Once the filing is older than a door can act, the finish door sends what the filing names wherever the store shows
the change landed, as the event the door built: a control event under the id the door gave it, a lineage marker at the
time the door fixed, which its run id is derived from. A change the store shows did not land is announced by nobody.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own ([[LH-235]]), on a real ``dir`` namespace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, Unpack

import lance
import pyarrow as pa
import pytest
from announcement_filings import age_filings

from catalog.api.dependencies import get_control_emitter
from catalog.core.config import get_settings
from catalog.core.lineage_emit import EmitFields
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.control_events import CatalogControlEvent


ADMIN, MEMBER, RECLAIMER = "alice", "carol", "maintenance"
_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1, 2], pa.int64())}))


class _Stopped(Exception):
    """The process a door ran in ended at the send it raises from."""


class _Lanes:
    """The control lane and the lineage lane, as what each was sent. While ``stopping`` is set, the first send on either
    is recorded and then stops the door, as a process ending right before its announcement went out would."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []
        self.markers: list[tuple[str, str, str | None, str | None]] = []
        self.stopping = False

    def _stop(self) -> None:
        if self.stopping:
            self.stopping = False
            raise _Stopped

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)
        self._stop()

    async def project_for(self, top_ns: str) -> str | None:
        del top_ns
        return None

    async def emit_create(self, *, table_id: str, namespace: str, author: str | None, version: int, **fields: Unpack[EmitFields]) -> None:
        del table_id, namespace, author, version, fields

    async def emit_write(
        self,
        *,
        table_id: str,
        namespace: str,
        author: str | None,
        version: int | None,
        operation: str,
        **fields: Unpack[EmitFields],
    ) -> None:
        del namespace, version
        uuids = fields.get("uuids")
        self.markers.append((operation, table_id, fields.get("event_time"), uuids.table_uuid if uuids is not None else None))
        self._stop()


@pytest.fixture
def estate(governed_catalog: Any) -> Iterator[tuple[Any, _Lanes]]:  # noqa: ANN401 — the conftest's GovernedCatalog
    """alice's project acme with carol its member, alice's namespace ``db``, and maintenance holding the estate's
    ``reclaimer`` role, which the finish door asks; both lanes recorded from here on."""
    catalog = governed_catalog
    catalog.store.write((catalog.person(ADMIN), "admin", "estate:rask"), (catalog.person(RECLAIMER), "reclaimer", "estate:rask"))
    for path, body in (("/management/v1/projects", {"id": "acme"}), ("/v1/namespace/db/create", {})):
        made = catalog.client.post(path, json=body, headers=catalog.bearer(ADMIN))
        assert made.status_code == 200, made.text
    catalog.store.write((catalog.person(MEMBER), "member", "project:acme"))
    lanes = _Lanes()
    app = catalog.client.app
    kept = app.state.lineage_emitter
    app.dependency_overrides[get_control_emitter] = lambda: lanes
    app.state.lineage_emitter = lanes
    yield catalog, lanes
    app.state.lineage_emitter = kept


def _stopped_after_its_change(catalog: Any, lanes: _Lanes, method: str, path: str, body: dict[str, Any]) -> int | None:  # noqa: ANN401 — the conftest's GovernedCatalog
    """Call the door with its first send stopping it, then age what it filed past the lease; the door's status, ``None``
    for a door that stopped."""
    lanes.stopping = True
    try:
        status: int | None = catalog.client.request(method, path, json=body, headers=catalog.bearer(ADMIN)).status_code
    except _Stopped:
        status = None
    finally:
        lanes.stopping = False
    age_filings()
    return status


def _finish(catalog: Any) -> dict[str, int]:  # noqa: ANN401 — the conftest's GovernedCatalog
    finished = catalog.client.post("/management/v1/announcement-intents/finish", headers=catalog.bearer(RECLAIMER))
    assert finished.status_code == 200, finished.text
    return finished.json()


@pytest.mark.parametrize(
    ("revoked", "answered", "finished"),
    [
        pytest.param(MEMBER, None, {"finished": 1, "sent": 1, "unlanded": 0, "young": 0, "left": 0}, id="a-revoke-that-landed"),
        pytest.param(ADMIN, 409, {"finished": 1, "sent": 0, "unlanded": 1, "young": 0, "left": 0}, id="the-last-admin-the-door-refused"),
    ],
)
def test_a_revoke_whose_door_stopped_after_it_is_announced_by_the_finish(
    estate: tuple[Any, _Lanes],
    revoked: str,
    answered: int | None,
    finished: dict[str, int],
) -> None:
    """A revoke that landed tells its person once, under the id its door filed it with; one the door refused, which left
    its filing too, tells nobody."""
    catalog, lanes = estate
    relation = "member" if revoked == MEMBER else "admin"

    status = _stopped_after_its_change(catalog, lanes, "DELETE", "/management/v1/projects/acme/members", {"user": f"dex~{revoked}", "relation": relation})
    attempted = list(lanes.events)

    assert (status, _finish(catalog)) == (answered, finished)
    told = [(event.action, event.event_id, event.extra.get("subject")) for event in lanes.events]
    assert told == [(event.action, event.event_id, event.extra.get("subject")) for event in attempted] * 2
    assert told == ([("grant_revoked", attempted[0].event_id, catalog.person(MEMBER))] * 2 if revoked == MEMBER else [])
    assert _finish(catalog) == {"finished": 0, "sent": 0, "unlanded": 0, "young": 0, "left": 0}


def _declared(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    del catalog


def _created(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    made = catalog.client.post("/v1/table/db$t/create", content=_ROWS, headers={**catalog.bearer(ADMIN), "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert made.status_code == 200, made.text


def _written_outside(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    del catalog
    lance.write_dataset(
        pa.table({"id": pa.array([1, 2], pa.int64())}), f"{get_settings().root}/outside/t", data_storage_version="2.2", enable_stable_row_ids=True
    )


def _nested(catalog: Any) -> None:  # noqa: ANN401 — the conftest's GovernedCatalog
    made = catalog.client.post("/v1/namespace/db$sub/create", json={}, headers=catalog.bearer(ADMIN))
    assert made.status_code == 200, made.text
    made = catalog.client.post("/v1/table/db$sub$t/create", content=_ROWS, headers={**catalog.bearer(ADMIN), "Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert made.status_code == 200, made.text


@pytest.mark.parametrize(
    ("arranged", "path", "body", "operation", "named", "told", "unlanded"),
    [
        pytest.param(_declared, "/v1/table/db$t/declare", {}, "declare_table", "db$t", "table_declared", 0, id="declare"),
        pytest.param(_written_outside, "/v1/table/db$t/register", {"location": "outside/t"}, "register_table", "db$t", "table_registered", 0, id="register"),
        pytest.param(_created, "/v1/table/db$t/rename", {"new_table_name": "u"}, "rename_table", "db$u", "table_renamed", 0, id="rename"),
        pytest.param(_created, "/v1/table/db$t/drop", {}, "drop_table", "db$t", "table_dropped", 0, id="drop"),
        # Stopped at its one table's marker, a cascade has dropped that table and not yet its namespace.
        pytest.param(_nested, "/v1/namespace/db$sub/drop", {"behavior": "Cascade"}, "drop_table", "db$sub$t", None, 1, id="namespace-cascade"),
    ],
)
def test_a_tables_lifecycle_door_that_stopped_after_its_change_is_announced_by_the_finish(
    estate: tuple[Any, _Lanes],
    arranged: Callable[[Any], None],
    path: str,
    body: dict[str, Any],
    operation: str,
    named: str,
    told: str | None,
    unlanded: int,
) -> None:
    """A table declared, registered, renamed or dropped, on its own or by its namespace's cascade, by a door that stopped
    before announcing it reaches lineage as the run the door would have recorded, and the control plane as the event the
    door would have sent, once; what the door had not done yet is announced by nobody."""
    catalog, lanes = estate
    arranged(catalog)
    lanes.markers.clear()
    lanes.events.clear()

    status = _stopped_after_its_change(catalog, lanes, "POST", path, body)
    attempted = list(lanes.markers)

    sent = 2 - unlanded
    assert (status, _finish(catalog)) == (None, {"finished": 1, "sent": sent, "unlanded": unlanded, "young": 0, "left": 0})
    assert [marker[:2] for marker in attempted] == [(operation, named)]
    assert lanes.markers == attempted * 2, "the finish's marker is another run than the one the door filed"
    expected = [(told, named, f"table:{attempted[0][3]}")] if told is not None else []
    assert [(event.action, event.object_name, event.object_id) for event in lanes.events] == expected
