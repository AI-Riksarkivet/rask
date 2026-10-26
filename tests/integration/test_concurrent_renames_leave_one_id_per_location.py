"""Concurrent renames of one table leave one live id on its location ([[LH-204]]).

The `dir` backend arbitrates only ADDs of an object id: measured on pylance 12.0.0 (lh204 m1), eight
barrier-threaded renames of one source, each retiring the source before registering its destination,
all answered 200 and left eight live ids on one dataset in 4 of 4 rounds. A destructive drop of any of
them then deletes the bytes the other seven resolve to. Renames into ONE destination name race the same
way: each must tell its own claim from another request's, and a loser must not put the source back over
the winner's finished rename.

Driven through the real catalog app on a real `dir` namespace, with every rename released by one
barrier, and judged by what the catalog then resolves.

A drop or deregister racing a rename acts only on the table it resolved ([[LH-521]], ADR 0173 §7). Both
are addressed by name, so a rename of the table followed by a create at the same name, landing between
the door's resolution and its backend call, would hand the door the new table. The rival is landed at
the two points that matter, each on every run: right after the door reads its location's claim (the
door must answer 409) and as the door calls the backend (the rename must answer 409, and the door acts
on the table it resolved).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from typing import Any, cast

import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from lance_namespace import ConcurrentModificationError, LanceNamespace, TableAlreadyExistsError

from service_kit.lakehouse import location_claims, records
from service_kit.lakehouse.objectfs import StorageOptions


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
RENAMES = 8


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


@pytest.mark.parametrize("same_destination", [pytest.param(False, id="distinct-destinations"), pytest.param(True, id="one-destination")])
def test_concurrent_renames_of_one_table_leave_one_live_id_on_its_location(real_ns_client: TestClient, same_destination: bool) -> None:
    client = real_ns_client
    assert client.post("/v1/namespace/ns1/create", json={}).status_code == 200
    created = client.post("/v1/table/ns1$src/create?mode=create", content=_ipc(pa.table({"id": [1, 2, 3]})), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    barrier = threading.Barrier(RENAMES)
    answers: dict[int, tuple[int, dict[str, object]]] = {}

    def _rename(i: int) -> None:
        barrier.wait()
        response = client.post("/v1/table/ns1$src/rename", json={"new_table_name": "d0" if same_destination else f"d{i}"})
        answers[i] = (response.status_code, response.json())

    threads = [threading.Thread(target=_rename, args=(i,)) for i in range(RENAMES)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    resolving = {}
    for name in ["src", *(f"d{i}" for i in range(RENAMES))]:
        described = client.post(f"/v1/table/ns1${name}/describe", json={})
        if described.status_code == 200:
            resolving[name] = described.json()["location"]
    statuses = sorted(status for status, _ in answers.values())
    losers = [body for status, body in answers.values() if status == 409]
    assert len(resolving) == 1, f"{len(resolving)} live ids after {RENAMES} concurrent renames: {resolving}"
    assert list(resolving.values()) == [location]
    # A loser that reaches the door after the winner has finished finds no source: 404, also a refusal. One that
    # resolved the source before the winner finished finds the destination it named taken, when it is the winner's.
    assert len(statuses) == RENAMES and statuses.count(200) == 1 and set(statuses) <= {200, 404, 409}, answers
    refusals = {ConcurrentModificationError.code, TableAlreadyExistsError.code} if same_destination else {ConcurrentModificationError.code}
    assert {body.get("code") for body in losers} <= refusals, losers
    # A loser learns that the source moved, never the name another request moved it to ([[LH-436]]).
    assert {body.get("detail") for body in losers} <= {
        "cannot rename ns1$src: its location is held by another table, by a concurrent rename or registration; describe it and retry",
        "cannot rename ns1$src: a concurrent rename already moved it to ns1$d0",
        "table already exists: ns1.d0",
    }, losers


class _RivalBefore:
    """The real namespace, landing ``rival`` once, just before the first call of ``method`` reaches it."""

    def __init__(self, ns: LanceNamespace, method: str, rival: Callable[[], None]) -> None:
        self._ns, self._method, self._rival = ns, method, rival

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — forwards the backend's own methods
        real = getattr(self._ns, name)
        if name != self._method:
            return real

        def _call(*args: object, **kwargs: object) -> object:
            self._rival()
            return real(*args, **kwargs)

        return _call


@pytest.mark.parametrize(
    ("door", "act", "trash_grace_days"),
    [
        pytest.param("drop", "deregister_table", "0", id="drop"),
        pytest.param("drop", "deregister_table", "7", id="recoverable-drop"),
        pytest.param("deregister", "deregister_table", "0", id="deregister"),
    ],
)
@pytest.mark.parametrize("rival_lands", ["after-the-door-read-the-claim", "as-the-door-acts"])
def test_a_door_racing_a_rename_acts_only_on_the_table_it_resolved(
    real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, door: str, act: str, trash_grace_days: str, rival_lands: str
) -> None:
    # The app module builds its settings at import, so it is imported once the fixture has pinned the environment.
    from catalog.api.dependencies import get_namespace
    from catalog.core.config import get_settings
    from catalog.main import app

    client = real_ns_client
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", trash_grace_days)
    get_settings.cache_clear()
    assert client.post("/v1/namespace/ns1/create", json={}).status_code == 200
    created = client.post("/v1/table/ns1$src/create?mode=create", content=_ipc(pa.table({"id": [1, 2, 3]})), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    pending = [True]
    rival: list[int] = []

    def _rename_then_recreate() -> None:
        # One shot, and disarmed before it sends anything: the rival's own requests read the claim and call the backend.
        if not pending:
            return
        pending.clear()
        moved = client.post("/v1/table/ns1$src/rename", json={"new_table_name": "moved"})
        rival.append(moved.status_code)
        if moved.status_code == 200:
            again = client.post("/v1/table/ns1$src/create?mode=create", content=_ipc(pa.table({"id": [9]})), headers=ARROW)
            rival.append(again.status_code)

    if rival_lands == "as-the-door-acts":
        real_ns = cast(LanceNamespace, app.dependency_overrides[get_namespace]())
        racing = _RivalBefore(real_ns, act, _rename_then_recreate)
        app.dependency_overrides[get_namespace] = lambda: racing
    else:
        read, key = records.read_json_at, location_claims.claim_key(location)

        def _read_then_race(root_uri: str, storage_options: StorageOptions, at: str) -> records.StoreRead:
            found = read(root_uri, storage_options, at)
            if at == key:
                _rename_then_recreate()
            return found

        monkeypatch.setattr(records, "read_json_at", _read_then_race)

    answered = client.post(f"/v1/table/ns1$src/{door}", json={})

    resolving = {}
    for name in ("src", "moved"):
        described = client.post(f"/v1/table/ns1${name}/describe", json={})
        if described.status_code == 200:
            resolving[name] = described.json()["location"]
    if rival_lands == "as-the-door-acts":
        # The door holds the claim it resolved while it acts, so the rename loses and the door acts on that table.
        assert rival == [409], f"the rival rename answered {rival} while the {door} was acting"
        assert answered.status_code == 200, answered.text
        assert resolving == {}, f"the {door} acted on another table: {resolving}"
    else:
        # The rename landed after the door read the claim, so the door answers 409 and both tables stand.
        assert rival == [200, 200], f"the rival never landed after the {door} read the claim: {rival}"
        assert answered.status_code == 409, f"the {door} acted on the table the name held after it resolved: {answered.text}"
        assert answered.json().get("code") == ConcurrentModificationError.code, answered.json()
        assert resolving.get("moved") == location and resolving.get("src") not in (None, location), resolving
