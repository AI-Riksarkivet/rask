"""What a table lifecycle door answers 200 for stays true while another door acts on the same name (ADR 0227).

Each test drives one candidate finding of the lifecycle model (`specs/catalog/TableLifecycle.tla`) through the real
catalog app on a real pylance `dir` namespace: one request is held at the step the model's trace names (a backend call,
the Lance write, the trash clear), the other door runs whole, the held request is released, and the claim is judged by
what a client then reads over HTTP. A failure the trace needs (a rename's register, a create's write, a recoverable
drop's deregister) is the backend or Lance raising at that step.

Every test is marked xfail, strictly: each reproduces a defect of the catalog, confirmed 2026-11-17, whose fix is the
owner's to schedule. The day a fix lands the test passes and the strict marker turns the run red, so the marker goes
with the fix. ``raises=AssertionError`` keeps a scenario that no longer sets up (``pytest.fail``) a failure of its own.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace, ServiceUnavailableError

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


T = "lance_catalog$ns1$t"
ARROW = {"Content-Type": ARROW_STREAM_MEDIA_TYPE}
#: How long one request waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 30


def _confirmed(finding: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{finding}: ADR 0227, confirmed 2026-11-17; the fix is the owner's to schedule")


class _Gate:
    """One step of a request, held until ``release`` is set; it then raises ``fail`` when given, the step failing."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.arrived, self.release, self._fail = threading.Event(), threading.Event(), fail

    def hold(self) -> None:
        self.arrived.set()
        if not self.release.wait(WAIT):
            raise TimeoutError("the held step was never released")
        if self._fail is not None:
            raise self._fail


class _Holding:
    """The real namespace, holding the first call of a method whose request names a table, at a gate of its own."""

    def __init__(self, ns: LanceNamespace) -> None:
        self._ns = ns
        self._gates: list[tuple[str, str, _Gate, list[bool]]] = []

    def at(self, method: str, table: str, gate: _Gate) -> _Gate:
        self._gates.append((method, table, gate, [True]))
        return gate

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — forwards the backend's own methods
        real = getattr(self._ns, name)

        def _call(request: Any, *args: object, **kwargs: object) -> object:  # noqa: ANN401 — the backend's own request types
            for method, table, gate, pending in self._gates:
                if method == name and pending and list(request.id)[-1] == table:
                    pending.clear()
                    gate.hold()
                    break
            return real(request, *args, **kwargs)

        return _call


def _holding() -> _Holding:
    from catalog.api.dependencies import get_namespace
    from catalog.main import app

    held = _Holding(cast(LanceNamespace, app.dependency_overrides[get_namespace]()))
    app.dependency_overrides[get_namespace] = lambda: held
    return held


class _Request:
    """A request sent on a thread of its own; a server fault the client raises is kept as the 500 it answers."""

    def __init__(self, send: Callable[[], Any]) -> None:
        self.status: int | None = None
        self.body: dict[str, Any] = {}

        def _run() -> None:
            try:
                answered = send()
            except Exception:  # noqa: BLE001 — the client re-raises a fault the app answered 500
                self.status = 500
                return
            self.status, self.body = answered.status_code, answered.json()

        self._thread = threading.Thread(target=_run)
        self._thread.start()

    def reaches(self, gate: _Gate) -> None:
        """Wait until the request is held at ``gate`` or has ended without reaching it."""
        for _ in range(WAIT * 20):
            if gate.arrived.wait(0.05) or not self._thread.is_alive():
                return

    def answer(self) -> int | None:
        self._thread.join(WAIT)
        return self.status


def _create(client: TestClient, ids: list[int], name: str = T) -> Any:  # noqa: ANN401 — the client's response
    return client.post(f"/v1/table/{name}/create?mode=create", content=encode_arrow_stream(pa.table({"id": pa.array(ids, pa.int64())})), headers=ARROW)


def _table(client: TestClient, name: str = T) -> tuple[str | None, int | None]:
    """The uuid the catalog describes ``name`` under and the rows it counts there; ``None`` for what it does not answer."""
    described = client.post(f"/v1/table/{name}/describe", json={})
    if described.status_code != 200:
        return None, None
    counted = client.post(f"/v1/table/{name}/count_rows", json={})
    return described.json()["properties"]["rask.uuid"], int(counted.text) if counted.status_code == 200 else None


@pytest.fixture
def namespace(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The catalog with namespace ``lance_catalog$ns1`` and a grace period, so a drop without ``purge`` is recoverable."""
    from catalog.core.config import get_settings

    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    get_settings.cache_clear()
    assert real_ns_client.post("/v1/namespace/lance_catalog$ns1/create", json={}).status_code == 200
    return real_ns_client


@pytest.fixture
def written(namespace: TestClient) -> str:
    """``T`` written with three rows; its uuid."""
    created = _create(namespace, [1, 2, 3])
    assert created.status_code == 200, created.text
    uuid, rows = _table(namespace)
    assert uuid is not None and rows == 3
    return uuid


def _held_writes(monkeypatch: pytest.MonkeyPatch, *gates: _Gate, streamed: _Gate | None = None) -> None:
    """Hold the Nth Lance write at ``gates[N]``; with ``streamed``, the write after them is held inside Lance instead,
    after its first row, so it is past Lance's own check for a dataset at the location and before its commit."""
    real = lance.write_dataset
    calls: list[int] = []
    lock = threading.Lock()

    def _write(data: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — Lance's own argument types
        with lock:
            n = len(calls)
            calls.append(n)
        if n < len(gates):
            gates[n].hold()
            return real(data, *args, **kwargs)
        if streamed is None:
            return real(data, *args, **kwargs)
        batches = cast(pa.Table, data).to_batches(max_chunksize=1)

        def _rows() -> Any:  # noqa: ANN401 — a generator of record batches
            yield batches[0]
            streamed.hold()
            yield from batches[1:]

        return real(pa.RecordBatchReader.from_batches(cast(pa.Table, data).schema, _rows()), *args, **kwargs)

    monkeypatch.setattr(lance, "write_dataset", _write)


@_confirmed("TableLifecycleCreateRace")
def test_a_create_answered_200_stands_when_a_second_create_of_its_name_fails(namespace: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The second create probes while the first is declared and unwritten, so it takes the declared path and writes at
    # the first's location once the first has committed there: Lance refuses it, and its rollback is a DropTable by name.
    first_writes, second_writes = _Gate(), _Gate()
    _held_writes(monkeypatch, first_writes, second_writes)
    first = _Request(lambda: _create(namespace, [1, 2, 3]))
    first.reaches(first_writes)
    second = _Request(lambda: _create(namespace, [7, 8]))
    second.reaches(second_writes)
    first_writes.release.set()
    if first.answer() != 200:
        pytest.fail(f"the first create answered {first.status}: {first.body}")
    second_writes.release.set()
    second.answer()

    assert _table(namespace)[1] == 3, f"the table the first create answered 200 for is gone; the second answered {second.status}: {second.body}"


@_confirmed("TableLifecycleCreateBothAnswered")
def test_of_two_creates_of_one_name_only_the_one_whose_rows_the_table_holds_answers_200(namespace: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # The second create passes Lance's check for a dataset before the first commits and commits after it: Lance lands it
    # as the table's next version, over the first's rows.
    first_writes, second_streams = _Gate(), _Gate()
    _held_writes(monkeypatch, first_writes, streamed=second_streams)
    first = _Request(lambda: _create(namespace, [1, 2, 3]))
    first.reaches(first_writes)
    second = _Request(lambda: _create(namespace, [7, 8]))
    second.reaches(second_streams)
    first_writes.release.set()
    first.answer()
    second_streams.release.set()
    second.answer()

    answered = {rows: request.status for rows, request in ((3, first), (2, second))}
    _, held = _table(namespace)
    assert [rows for rows, status in answered.items() if status == 200] == [held], f"answered {answered}, and the table holds {held} rows"


@_confirmed("TableLifecycleUndropOverDestroyed")
def test_an_undrop_answered_200_never_names_destroyed_bytes(namespace: TestClient, written: str) -> None:
    held = _holding()
    # A recoverable drop whose deregister fails leaves its trash record on the live table, as one that dies there does.
    held.at("deregister_table", "t", _Gate(fail=ServiceUnavailableError("the backend is away"))).release.set()
    if (failed := namespace.post(f"/v1/table/{T}/drop", json={})).status_code == 200:
        pytest.fail(f"the recoverable drop did not fail at its deregister: {failed.text}")
    registers = held.at("register_table", "t", _Gate())
    undrop = _Request(lambda: namespace.post(f"/management/v1/table/{T}/undrop"))
    undrop.reaches(registers)
    destroyed = namespace.post(f"/v1/table/{T}/drop?purge=true", json={})
    registers.release.set()
    undrop.answer()

    uuid, rows = _table(namespace)
    assert uuid is None or rows == 3, (
        f"the undrop answered {undrop.status} and the name stands over no data; the destroying drop answered {destroyed.status_code}"
    )


@_confirmed("TableLifecycleSoftDropWrongRow")
def test_a_recoverable_drop_answered_200_leaves_every_table_it_detached_restorable(
    namespace: TestClient, written: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _holding()
    renames = held.at("register_table", "t2", _Gate(fail=ServiceUnavailableError("the backend is away")))
    creates = _Gate(fail=OSError("the store is away"))
    _held_writes(monkeypatch, creates)
    # The rename detaches the table and is held at its register; a create of the freed name is held at its write; the
    # recoverable drop resolves the created table, files its record and is held at its deregister.
    rename = _Request(lambda: namespace.post(f"/v1/table/{T}/rename", json={"new_table_name": "t2"}))
    rename.reaches(renames)
    create = _Request(lambda: _create(namespace, [9]))
    create.reaches(creates)
    detaches = held.at("deregister_table", "t", _Gate())
    drop = _Request(lambda: namespace.post(f"/v1/table/{T}/drop", json={}))
    drop.reaches(detaches)
    # The create's write fails and its rollback removes its row; the rename's register fails and it puts the table back
    # under the name; the drop then detaches the row the name holds.
    creates.release.set()
    create.answer()
    renames.release.set()
    rename.answer()
    detaches.release.set()
    if drop.answer() != 200:
        pytest.fail(f"the recoverable drop answered {drop.status}: {drop.body}")
    restored = namespace.post(f"/management/v1/table/{T}/undrop")

    named = {name: _table(namespace, name) for name in (T, "lance_catalog$ns1$t2")}
    assert (written, 3) in named.values(), f"the table no name and no trash record keeps: undrop answered {restored.status_code}, the names hold {named}"


@_confirmed("TableLifecycleUndropRace")
def test_an_undrop_answered_200_restores_the_dropped_table_under_its_name(namespace: TestClient, written: str) -> None:
    if (dropped := namespace.post(f"/v1/table/{T}/drop", json={})).status_code != 200:
        pytest.fail(f"the recoverable drop answered {dropped.status_code}: {dropped.text}")
    held = _holding()
    registers = held.at("register_table", "t", _Gate())
    undrop = _Request(lambda: namespace.post(f"/management/v1/table/{T}/undrop"))
    undrop.reaches(registers)
    created = _create(namespace, [9])
    registers.release.set()
    undrop.answer()

    assert undrop.status != 200 or _table(namespace) == (written, 3), (
        f"the undrop answered 200 and the name holds {_table(namespace)}, a table created as it ran ({created.status_code})"
    )


@_confirmed("TableLifecycleUndropClearsRedrop")
def test_a_recoverable_drop_answered_200_can_be_undropped(namespace: TestClient, written: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from lakehouse import trash

    if (dropped := namespace.post(f"/v1/table/{T}/drop", json={})).status_code != 200:
        pytest.fail(f"the recoverable drop answered {dropped.status_code}: {dropped.text}")
    clears, real_clear, pending = _Gate(), trash.clear, [True]

    def _clear(*args: Any, **kwargs: Any) -> bool:  # noqa: ANN401 — trash.clear's own arguments
        if pending:
            pending.clear()
            clears.hold()
        return real_clear(*args, **kwargs)

    monkeypatch.setattr(trash, "clear", _clear)
    # The undrop registers the table and is held before it clears the record; the table is dropped again meanwhile.
    undrop = _Request(lambda: namespace.post(f"/management/v1/table/{T}/undrop"))
    undrop.reaches(clears)
    redropped = namespace.post(f"/v1/table/{T}/drop", json={})
    clears.release.set()
    undrop.answer()
    restored = namespace.post(f"/management/v1/table/{T}/undrop")

    assert redropped.status_code != 200 or (restored.status_code, _table(namespace)) == (200, (written, 3)), (
        f"the second drop answered 200 and its undrop answered {restored.status_code}: {restored.text}"
    )


@_confirmed("TableLifecycleProtectRace")
def test_a_table_whose_protection_answered_200_is_not_destroyed_by_a_drop_without_force(namespace: TestClient, written: str) -> None:
    held = _holding()
    # The drop has read the protection record, fenced the claim and judged the bytes; it is held at its deregister.
    detaches = held.at("deregister_table", "t", _Gate())
    drop = _Request(lambda: namespace.post(f"/v1/table/{T}/drop?purge=true", json={}))
    drop.reaches(detaches)
    protected = namespace.post(f"/management/v1/table/{T}/protection", json={"protected": True})
    detaches.release.set()
    drop.answer()

    assert protected.status_code != 200 or _table(namespace) == (written, 3), (
        f"protection answered 200, then the drop answered {drop.status} and destroyed the table"
    )
