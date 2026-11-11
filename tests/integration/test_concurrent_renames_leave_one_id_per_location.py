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
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from lance_namespace import ConcurrentModificationError, InvalidInputError, LanceNamespace, TableAlreadyExistsError

from lakehouse import location_claims, records
from lakehouse.storage_options import StorageOptions


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
    created = client.post("/v1/table/ns1$first/create?mode=create", content=_ipc(pa.table({"id": [1, 2, 3]})), headers=ARROW)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    # Renamed once before the race, so the renames below start from the claim a finished rename leaves: handed to the
    # name the table has, and free of that rename's fence, or none of them could take it.
    assert client.post("/v1/table/ns1$first/rename", json={"new_table_name": "src"}).status_code == 200
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
    for name in ["first", "src", *(f"d{i}" for i in range(RENAMES))]:
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


#: Each door at each landing point, but a recoverable drop only where the rival lands as the door acts: one that lands
#: after the door read the claim is refused at the claim fence on a plain drop's branch, before the trash arm.
@pytest.mark.parametrize(
    ("rival_lands", "door", "act", "trash_grace_days"),
    [
        pytest.param("after-the-door-read-the-claim", "drop", "deregister_table", "0", id="after-the-door-read-the-claim-drop"),
        pytest.param("after-the-door-read-the-claim", "deregister", "deregister_table", "0", id="after-the-door-read-the-claim-deregister"),
        pytest.param("as-the-door-acts", "drop", "deregister_table", "0", id="as-the-door-acts-drop"),
        pytest.param("as-the-door-acts", "drop", "deregister_table", "7", id="as-the-door-acts-recoverable-drop"),
        pytest.param("as-the-door-acts", "deregister", "deregister_table", "0", id="as-the-door-acts-deregister"),
    ],
)
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


#: Where the renamed table is registered, beneath the namespace root: a register cannot name the uuid directory a create
#: lays its table out in, so only a registered table's location can be registered again.
_HELD = "held"

#: How long one request waits for the request it is interleaved with: far above what either takes, so it only bounds a hang.
_WAIT_SECONDS = 30


class _HeldOnceLanded:
    """The real namespace, holding the first call of ``method`` once it has landed, until ``release`` is set."""

    def __init__(self, ns: LanceNamespace, method: str) -> None:
        self._ns, self._method, self._pending = ns, method, True
        self.landed, self.release = threading.Event(), threading.Event()

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — forwards the backend's own methods
        real = getattr(self._ns, name)
        if name != self._method:
            return real

        def _call(*args: object, **kwargs: object) -> object:
            answer = real(*args, **kwargs)
            if self._pending:
                self._pending = False
                self.landed.set()
                self.release.wait(_WAIT_SECONDS)
            return answer

        return _call


#: A RegisterTable of the rename's destination, at the table's location, at each point of the rename where the claim
#: names a table the backend does not describe. Landed whole it is refused as a concurrent change and the rename goes
#: on. The register door attaches its row before the claim refuses it, so where that row still stands as the rename
#: registers its destination, the rename finds the name taken, puts the source back and answers the conflict.
@pytest.mark.parametrize(
    ("rival_lands", "its_row_stands", "rival_answer", "rename_answer", "named"),
    [
        pytest.param("deregister_table", False, (409, ConcurrentModificationError.code), (200, None), "dst", id="before-the-rename-retires-the-source"),
        pytest.param("register_table", False, (409, ConcurrentModificationError.code), (200, None), "dst", id="before-the-rename-registers-the-destination"),
        pytest.param(
            "deregister_table",
            True,
            (400, InvalidInputError.code),
            (409, TableAlreadyExistsError.code),
            "src",
            id="with-its-row-standing-as-the-rename-registers-the-destination",
        ),
    ],
)
def test_a_register_of_a_renames_destination_landed_inside_the_rename_takes_nothing_from_the_table(
    real_ns_client: TestClient,
    tmp_path: Path,
    rival_lands: str,
    its_row_stands: bool,
    rival_answer: tuple[int, int],
    rename_answer: tuple[int, int | None],
    named: str,
) -> None:
    # The app module builds its settings at import, so it is imported once the fixture has pinned the environment.
    from catalog.api.dependencies import get_namespace
    from catalog.main import app

    client = real_ns_client
    assert client.post("/v1/namespace/ns1/create", json={}).status_code == 200
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), str(tmp_path / _HELD), data_storage_version="2.2", enable_stable_row_ids=True)
    registered = client.post("/v1/table/ns1$src/register", json={"location": _HELD})
    assert registered.status_code == 200, registered.text
    described = client.post("/v1/table/ns1$src/describe", json={}).json()
    location, table_uuid = described["location"], described["properties"]["rask.uuid"]
    rival: list[tuple[int, object]] = []

    def _register_the_destination() -> None:
        answered = client.post("/v1/table/ns1$dst/register", json={"location": _HELD})
        rival.append((answered.status_code, answered.json().get("code")))

    registering = threading.Thread(target=_register_the_destination)
    attached = _HeldOnceLanded(cast(LanceNamespace, app.dependency_overrides[get_namespace]()), "register_table")
    pending = [True]

    def _land_the_registration() -> None:
        # One shot, and disarmed before the rival sends anything: its own request calls the backend through this too.
        if not pending:
            return
        pending.clear()
        registering.start()
        if its_row_stands:
            assert attached.landed.wait(_WAIT_SECONDS), "the registration never attached its row"
        else:
            attached.release.set()
            registering.join(_WAIT_SECONDS)

    racing = _RivalBefore(cast(LanceNamespace, attached), rival_lands, _land_the_registration)
    app.dependency_overrides[get_namespace] = lambda: racing

    renamed = client.post("/v1/table/ns1$src/rename", json={"new_table_name": "dst"})

    attached.release.set()
    registering.join(_WAIT_SECONDS)
    resolving = {}
    for name in ("src", "dst"):
        found = client.post(f"/v1/table/ns1${name}/describe", json={})
        if found.status_code == 200:
            resolving[name] = (found.json()["location"], found.json()["properties"]["rask.uuid"])
    # The registrant gains nothing: the one table at the location is the one that was there, under the uuid it had, so
    # every grant held on it still reaches it.
    assert (rival, (renamed.status_code, renamed.json().get("code")), resolving) == ([rival_answer], rename_answer, {named: (location, table_uuid)}), (
        renamed.text
    )
