"""A write sent under an Idempotency-Key acts once, and its answers say so, while its first attempt still runs beside the
resend that took its key over, at every door that takes a key (ADR 0240, `specs/catalog/CommitPath.tla`).

The request deadline answers a request 503 at 300 s and lets its door run on (`lakehouse.deadline`), and a key's claim
lapses after its lease, also 300 s (`lakehouse.idempotency.LEASE`): a resend can take the key over from an attempt that is
still running. The model's trace is driven step by step through the real catalog app on a real pylance namespace: the
first attempt is held after its claim and before its write, the resend runs whole under a lease driven to zero (the
lease having passed), another write lands, and the first attempt is released. What a client then reads over HTTP is
judged: the write landed once, each answer is true, and the key answers the resend's success from then on.

Each runs on the local root, and on a real S3 store where `make lifecycle-rustfs-check` names one (``race_ns_client``).
"""

from __future__ import annotations

import importlib
import inspect
import threading
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from typing import Any, NamedTuple

import httpx
import pyarrow as pa
import pytest
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


TABLE = "lance_catalog$ns1$t"
#: How long one request waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 30
_KEY = {"lance-schema:unenforced-primary-key": "true"}
_ARROW = {"Content-Type": ARROW_STREAM_MEDIA_TYPE}
_KEYED = "0b9e6a2c-5d1f-4e3a-9c7b-2a4d6e8f1c30"


def _rows(ids: list[int], *, keyed: bool = False) -> bytes:
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata=_KEY if keyed else None)])
    return encode_arrow_stream(pa.table({"id": pa.array(ids, pa.int64())}, schema=schema))


def _ok(answer: httpx.Response) -> httpx.Response:
    assert answer.status_code == 200, answer.text
    return answer


def _create(client: TestClient, ids: list[int], *, name: str = TABLE, keyed: bool = False) -> httpx.Response:
    return _ok(client.post(f"/v1/table/{name}/create", content=_rows(ids, keyed=keyed), headers=_ARROW))


def _append(client: TestClient, ids: list[int]) -> httpx.Response:
    return _ok(client.post(f"/v1/table/{TABLE}/insert?mode=append", content=_rows(ids), headers=_ARROW))


def _held(target: str, monkeypatch: pytest.MonkeyPatch) -> tuple[threading.Event, threading.Event]:
    """Hold the first call of ``target`` (``module:function``, a door's step after its claim) until released."""
    module_name, name = target.split(":")
    module = importlib.import_module(module_name)
    real = getattr(module, name)
    arrived, release = threading.Event(), threading.Event()

    def wait_once() -> None:
        if not arrived.is_set():
            arrived.set()
            if not release.wait(WAIT):
                raise TimeoutError("the first attempt was never released")

    if inspect.iscoroutinefunction(real):

        async def held_async(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — forwards the door's own call
            await run_in_threadpool(wait_once)
            return await real(*args, **kwargs)

        monkeypatch.setattr(module, name, held_async)
    else:

        def held(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — forwards the door's own call
            wait_once()
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, held)
    return arrived, release


def _lapsed(monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401 — monkeypatch's own context
    """Within the block every claim of a key is judged past its lease, as a resend after the lease has passed is."""
    from catalog.api import idempotency as keyed_doors
    from lakehouse import idempotency

    context = monkeypatch.context()
    lapsed = context.__enter__()
    lapsed.setattr(keyed_doors.idempotency, "claim", partial(idempotency.claim, lease=timedelta(0)))
    return context


def _interleave(
    monkeypatch: pytest.MonkeyPatch, hold: str, send: Callable[[], httpx.Response], between: Callable[[], object]
) -> tuple[httpx.Response, httpx.Response, httpx.Response]:
    """The first attempt held at ``hold``; the resend run whole past the lease; ``between``; the first released; a third
    send. Each answer, in that order."""
    arrived, release = _held(hold, monkeypatch)
    first: dict[str, httpx.Response] = {}
    thread = threading.Thread(target=lambda: first.update(answer=send()))
    thread.start()
    assert arrived.wait(WAIT), "the first attempt never reached its write"
    context = _lapsed(monkeypatch)
    try:
        resent = send()
    finally:
        context.__exit__(None, None, None)
    between()
    release.set()
    thread.join(WAIT)
    return first["answer"], resent, send()


def _count(client: TestClient, name: str = TABLE) -> str:
    return _ok(client.post(f"/v1/table/{name}/count_rows", json={})).text


def _version(client: TestClient) -> int:
    return int(_ok(client.post(f"/v1/table/{TABLE}/describe", json={"load_detailed_metadata": True})).json()["version"])


class _Door(NamedTuple):
    """One keyed door's race: the table before it (what it answers is handed to the request), the step its first attempt
    is held at, the keyed request, what lands between the resend and the first attempt's release, what the first attempt
    is answered, and what the table holds."""

    setup: Callable[[TestClient], object]
    hold: str
    send: Callable[[TestClient, Any], httpx.Response]
    between: Callable[[TestClient], object]
    first: tuple[int, int | None]
    holds: Callable[[TestClient], object]


def _keyed(client: TestClient, path: str, **kwargs: Any) -> httpx.Response:  # noqa: ANN401 — the request's own body
    headers = {**kwargs.pop("headers", {}), "Idempotency-Key": _KEYED}
    return client.post(path, headers=headers, **kwargs)


def _register_setup(client: TestClient) -> str:
    """A dataset written outside the catalog under its root: its location, as the register door takes it."""
    import lance

    from catalog.core.config import get_settings

    settings = get_settings()
    lance.write_dataset(  # noqa: TID251 — a dataset no door made, for the register door to attach
        pa.table({"id": pa.array([1, 2, 3], pa.int64())}),
        f"{settings.root.rstrip('/')}/outside/registered",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        storage_options=settings.storage_options().in_clear() or None,
    )
    return "outside/registered"


def _gone(client: TestClient, name: str = TABLE) -> None:
    assert client.post(f"/v1/table/{name}/describe", json={}).status_code == 404


def _rows_and_version(count: str, version: int) -> Callable[[TestClient], None]:
    def holds(client: TestClient) -> None:
        assert (_version(client), _count(client)) == (version, count), "the keyed write acted twice"

    return holds


_DOORS = {
    # An Overwrite landed again after the resend's would erase the append between them (guard KeyRenews, at the create
    # door's Lance commit).
    "create-overwrite": _Door(
        setup=lambda client: _create(client, [1, 2, 3]),
        hold="catalog.services.table_create:create_governed_table",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/create?mode=Overwrite", content=_rows([7, 8]), headers=_ARROW),
        between=lambda client: _append(client, [9]),
        first=(409, 14),
        holds=_rows_and_version("3", 3),
    ),
    # So would a restore landed again (guard KeyRenews, at the restore's stamped commit).
    "restore": _Door(
        setup=lambda client: (_create(client, [1, 2, 3]), _append(client, [4])),
        hold="catalog.services.dataplane:restore",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/restore", json={"version": 1}),
        between=lambda client: _append(client, [9]),
        first=(409, 14),
        holds=_rows_and_version("4", 4),
    ),
    # A create landed again would bring back the table dropped since (guard KeyRenews, at the create's first commit).
    "create": _Door(
        setup=lambda client: None,
        hold="catalog.services.table_create:create_governed_table",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/create", content=_rows([1, 2, 3]), headers=_ARROW),
        between=lambda client: _ok(client.post(f"/v1/table/{TABLE}/drop?purge=true", json={})),
        first=(409, 14),
        holds=_gone,
    ),
    # An ExistOk that finds the table answers its version, which is true, and leaves the key's answer the resend's
    # (guard RecordOwnClaim): the third send is answered the resend's version 1, not 2.
    "create-exist-ok": _Door(
        setup=lambda client: None,
        hold="catalog.services.table_create:create_governed_table",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/create?mode=ExistOk", content=_rows([1, 2, 3]), headers=_ARROW),
        between=lambda client: _append(client, [9]),
        first=(200, None),
        holds=_rows_and_version("4", 2),
    ),
    # The first attempt drops the table its name resolved to, which the resend dropped; the table created since stands.
    "drop": _Door(
        setup=lambda client: _create(client, [1, 2, 3]),
        hold="catalog.api.v1.endpoints.tables:_drop",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/drop?purge=true", json={}),
        between=lambda client: _create(client, [5, 6]),
        first=(409, 14),
        holds=lambda client: _count(client) == "2" or pytest.fail("the table created after the key's drop was dropped"),
    ),
    # The same for a deregister.
    "deregister": _Door(
        setup=lambda client: _create(client, [1, 2, 3]),
        hold="catalog.api.v1.endpoints.tables:_detach",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/deregister", json={}),
        between=lambda client: _create(client, [5, 6]),
        first=(409, 14),
        holds=lambda client: _count(client) == "2" or pytest.fail("the table created after the key's deregister was detached"),
    ),
    # A registration landed again would attach the table deregistered since (guard KeyRenews, before the backend's
    # register).
    "register": _Door(
        setup=_register_setup,
        hold="catalog.api.fga_deps:require_parent_exists",
        send=lambda client, location: _keyed(client, f"/v1/table/{TABLE}/register", json={"location": location}),
        between=lambda client: _ok(client.post(f"/v1/table/{TABLE}/deregister", json={})),
        first=(409, 14),
        holds=_gone,
    ),
    # A rename landed again would move the table back out from under the name it was renamed back to.
    "rename": _Door(
        setup=lambda client: _create(client, [1, 2, 3]),
        hold="catalog.services.dataplane:rename_table",
        send=lambda client, _setup: _keyed(client, f"/v1/table/{TABLE}/rename", json={"new_table_name": "u"}),
        between=lambda client: _ok(client.post("/v1/table/lance_catalog$ns1$u/rename", json={"new_table_name": "t"})),
        first=(409, 14),
        holds=lambda client: (_count(client) == "3" or pytest.fail("the table was renamed again"), _gone(client, "lance_catalog$ns1$u")),
    ),
}


@pytest.mark.parametrize("door", list(_DOORS))
def test_a_keyed_door_resent_while_its_first_attempt_runs_acts_once(race_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, door: str) -> None:
    race = _DOORS[door]
    client = race_ns_client
    _ok(client.post("/v1/namespace/lance_catalog$ns1/create", json={}))
    made = race.setup(client)
    first, resent, again = _interleave(monkeypatch, race.hold, lambda: race.send(client, made), lambda: race.between(client))
    assert resent.status_code == 200, resent.text
    assert (first.status_code, first.json().get("code")) == race.first, first.text
    assert (again.status_code, again.json()) == (200, resent.json()), "the key answers another attempt's write"
    race.holds(client)


@pytest.mark.parametrize("keyed", [pytest.param(False, id="no-key"), pytest.param(True, id="primary-key")])
def test_an_append_resent_under_its_key_while_its_first_attempt_runs_lands_once(
    race_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, keyed: bool
) -> None:
    """``no-key``, a table declaring no primary key (guard KeyRenews): the first attempt renews its claim right before its
    commit, finds the key the resend's, and answers 409 with nothing committed. Without the renewal it appended its rows
    a second time and answered 200. ``primary-key``, a table declaring one (guard RefusalHoldsKey): the first attempt's
    rows meet the key values the resend landed, and it answers 409, the key no longer its own. Without the guard it
    answered 400, "the table already holds a row with" its own key, for the write its key landed."""
    client = race_ns_client
    _ok(client.post("/v1/namespace/lance_catalog$ns1/create", json={}))
    _create(client, [1, 2, 3], keyed=keyed)
    body = _rows([7, 8], keyed=keyed)
    send = partial(_keyed, client, f"/v1/table/{TABLE}/insert?mode=append", content=body, headers=_ARROW)

    first, resent, again = _interleave(monkeypatch, "catalog.services.dataplane:insert_into_table", send, lambda: None)

    assert (resent.status_code, resent.json()) == (200, {"num_inserted_rows": 2, "version": 2}), resent.text
    assert (first.status_code, first.json().get("code")) == (409, 14), first.text
    assert (again.status_code, again.json()) == (200, resent.json()), again.text
    assert (_version(client), _count(client)) == (2, "5"), "the append landed twice"


def test_a_keyed_append_whose_landed_put_was_answered_lost_lands_once(race_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store can answer a conditional put that landed as a lost race (a put retried after its answer was lost meets its
    own object): Lance then reports a retryable conflict for the version it tried. A keyed append reads that version for
    its own transaction before it puts again, and answers it. Committing the same transaction again lands it a second
    time (measured on pylance 13.0.0), so without the read the rows landed twice."""
    from lance.commit import CommitConflictError

    from catalog.services import dataplane

    client = race_ns_client
    _ok(client.post("/v1/namespace/lance_catalog$ns1/create", json={}))
    _create(client, [1, 2, 3])
    commit = dataplane.lance.LanceDataset.commit
    answered: list[int] = []

    def landed_and_answered_lost(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — forwards Lance's own commit
        landed = commit(*args, **kwargs)
        if not answered and kwargs.get("max_retries") == 0:
            answered.append(int(landed.version))
            raise CommitConflictError(f"Commit conflict for version {landed.version}: Failed to commit the transaction after 0 retries.")
        return landed

    monkeypatch.setattr(dataplane.lance.LanceDataset, "commit", staticmethod(landed_and_answered_lost))
    sent = _keyed(client, f"/v1/table/{TABLE}/insert?mode=append", content=_rows([7, 8]), headers=_ARROW)

    assert answered == [2], "the store never answered a landed put as lost"
    assert (sent.status_code, sent.json()) == (200, {"num_inserted_rows": 2, "version": 2}), sent.text
    assert (_version(client), _count(client)) == (2, "5"), "the append landed twice"


def test_a_resend_that_took_the_key_over_keeps_it_while_the_first_attempt_reaches_its_commit(
    race_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The renewal finds the claim its own (guard RenewIsOwn, `CommitPathRenewNotOwn.cfg`): the first attempt is held
    before its write; the resend takes the key over, finds no commit, and is held before its write too; the first is
    released and reaches its commit while the resend still holds the key; the resend is released and commits. The first
    answers 409 with nothing committed and the resend 200. A renewal replacing whatever claim is in flight lets both
    commit: both answered 200 and the table held the rows twice."""
    from catalog.services import dataplane

    client = race_ns_client
    _ok(client.post("/v1/namespace/lance_catalog$ns1/create", json={}))
    _create(client, [1, 2, 3])
    send = partial(_keyed, client, f"/v1/table/{TABLE}/insert?mode=append", content=_rows([7, 8]), headers=_ARROW)
    writes = dataplane.insert_into_table
    gates = [(threading.Event(), threading.Event()) for _ in range(2)]
    order = iter(range(2))
    first_wrote, first_answers = threading.Event(), threading.Event()

    def held(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — forwards the dataplane's own call
        turn = next(order, None)
        if turn is None:
            return writes(*args, **kwargs)
        arrived, release = gates[turn]
        arrived.set()
        assert release.wait(WAIT)
        try:
            return writes(*args, **kwargs)
        finally:
            if turn == 0:
                # Past its commit, or refused before it: the first attempt waits to answer until the resend has.
                first_wrote.set()
                first_answers.wait(WAIT)

    monkeypatch.setattr(dataplane, "insert_into_table", held)
    answers: dict[str, httpx.Response] = {}
    first = threading.Thread(target=lambda: answers.update(first=send()))
    first.start()
    assert gates[0][0].wait(WAIT), "the first attempt never reached its write"
    context = _lapsed(monkeypatch)
    resend = threading.Thread(target=lambda: answers.update(resent=send()))
    resend.start()
    assert gates[1][0].wait(WAIT), "the resend never reached its write"
    context.__exit__(None, None, None)
    gates[0][1].set()
    assert first_wrote.wait(WAIT)
    gates[1][1].set()
    resend.join(WAIT)
    first_answers.set()
    first.join(WAIT)

    assert (answers["resent"].status_code, answers["resent"].json()) == (200, {"num_inserted_rows": 2, "version": 2}), answers["resent"].text
    assert (answers["first"].status_code, answers["first"].json().get("code")) == (409, 14), answers["first"].text
    assert (_version(client), _count(client)) == (2, "5"), "the append landed twice"
