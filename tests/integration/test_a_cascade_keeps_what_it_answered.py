"""What a namespace cascade and its undrop answer stays true when a step of theirs fails part-way (ADR 0233).

Each test drives one finding of the cascade model (`specs/catalog/Cascade.tla`) through the real catalog app on a real
pylance `dir` namespace: the step the model's trace ends at fails once (a trash record's write, a table's register), or
two requests reach the step that orders them together, the caller issues the request again as the door documents, and
the claim is judged by what a client then reads over HTTP. A request that dies at that step leaves the same state as one
whose step fails. Each finding was confirmed against the catalog on 2026-11-17 and is fixed (ADR 0233).
"""

from __future__ import annotations

import threading
from contextlib import suppress
from typing import Any, cast

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import LanceNamespace, ServiceUnavailableError

from lakehouse import trash
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.storage_options import StorageOptions


P = "lance_catalog$p"
C = f"{P}$c"
T = f"{C}$t"
#: How long one request waits for the one it is sent with: far above what either takes, so it only bounds a hang.
WAIT = 30
#: How long a request at the backend waits for the one it is sent with, which a fence may have answered already: far above
#: what reaching the backend takes, so two requests that both reach it meet.
MEET = 5


def _rows(client: TestClient, name: str = T) -> int | None:
    """The rows the catalog counts in ``name``; ``None`` when it does not answer for it."""
    counted = client.post(f"/v1/table/{name}/count_rows", json={})
    return int(counted.text) if counted.status_code == 200 else None


@pytest.fixture
def subtree(race_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """``p``, ``p$c`` and ``p$c$t`` with three rows, under a grace period, so a cascade without ``purge`` is recoverable."""
    from catalog.core.config import get_settings

    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    get_settings.cache_clear()
    for namespace in (P, C):
        assert race_ns_client.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
    rows = encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())}))
    created = race_ns_client.post(f"/v1/table/{T}/create?mode=create", content=rows, headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    assert _rows(race_ns_client) == 3
    return race_ns_client


def test_a_cascade_finished_by_its_reissue_leaves_every_table_it_detached_restorable(subtree: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # _trash_subtree files each namespace's record before its drop (G2): the record of `p$c` fails, nothing of `p$c` has
    # gone, and the re-issue files it and drops it, so the undrop of `p` rebuilds `p$c` for the table filed beneath it.
    real, pending = trash.put, [True]

    def _put(control_root: str, storage_options: StorageOptions, record: dict[str, Any]) -> None:
        if pending and record.get("kind") == "namespace" and record.get("id") == C:
            pending.clear()
            raise OSError("the control root did not answer")
        real(control_root, storage_options, record)

    monkeypatch.setattr(trash, "put", _put)
    cascade = {"behavior": "Cascade"}
    with pytest.raises(OSError, match="the control root did not answer"):
        # The client re-raises the fault the app answers 500 for.
        subtree.post(f"/v1/namespace/{P}/drop", json=cascade)
    finished = subtree.post(f"/v1/namespace/{P}/drop", json=cascade)
    undrop = subtree.post(f"/management/v1/namespace/{P}/undrop")

    assert _rows(subtree) == 3, (
        f"the re-issued cascade answered {finished.status_code} and the undrop {undrop.status_code}: {undrop.text}; "
        f"the undrop of {C} answers {subtree.post(f'/management/v1/namespace/{C}/undrop').status_code}, of {T} "
        f"{subtree.post(f'/management/v1/table/{T}/undrop').status_code}"
    )


class _FailsOnce:
    """The real namespace, its first ``method`` call naming ``table`` failing as a backend that does not answer."""

    def __init__(self, ns: LanceNamespace, method: str, table: str) -> None:
        self._ns, self._method, self._table, self._pending = ns, method, table, [True]

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — forwards the backend's own methods
        real = getattr(self._ns, name)

        def _call(request: Any, *args: object, **kwargs: object) -> object:  # noqa: ANN401 — the backend's own request types
            if name == self._method and self._pending and list(request.id)[-1] == self._table:
                self._pending.clear()
                raise ServiceUnavailableError("the backend is away")
            return real(request, *args, **kwargs)

        return _call


class _AtOnce:
    """The real namespace, sending its first ``create_namespace`` of ``child`` and its first ``drop_namespace`` of ``parent``
    to the backend together: each waits at a barrier until the other has reached it, or for :data:`MEET` when the other was
    answered before it reached the backend."""

    def __init__(self, ns: LanceNamespace, *, child: list[str], parent: list[str]) -> None:
        self._ns, self._calls = ns, {("create_namespace", tuple(child)), ("drop_namespace", tuple(parent))}
        self._barrier = threading.Barrier(2, timeout=MEET)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 — forwards the backend's own methods
        real = getattr(self._ns, name)

        def _call(request: Any, *args: object, **kwargs: object) -> object:  # noqa: ANN401 — the backend's own request types
            if (name, tuple(request.id or [])) in self._calls:
                self._calls.discard((name, tuple(request.id or [])))
                with suppress(threading.BrokenBarrierError):
                    self._barrier.wait()
            return real(request, *args, **kwargs)

        return _call


def test_a_namespace_created_beside_its_parents_drop_never_stands_without_it(subtree: TestClient) -> None:
    # The `dir` backend checks a new row's parent, and a Restrict drop the parent's rows, each against the __manifest it
    # read: two such commits sent together both land (measured on the installed pylance, 50 of 50), and the child stands
    # under no parent. Each holds `p2`'s namespace fence first (G1), so the one that takes it second is answered 409
    # before it reaches the backend, and the other goes on alone. `n` stands alone in `p2`, so the drop is a plain Restrict one.
    from catalog.api.dependencies import get_namespace
    from catalog.main import app

    assert subtree.post("/v1/namespace/lance_catalog$p2/create", json={}).status_code == 200
    ns = cast(LanceNamespace, app.dependency_overrides[get_namespace]())
    together = _AtOnce(ns, child=["lance_catalog", "p2", "n"], parent=["lance_catalog", "p2"])
    app.dependency_overrides[get_namespace] = lambda: together
    answers: dict[str, int] = {}

    def _send(label: str, path: str) -> None:
        answers[label] = subtree.post(path, json={}).status_code

    threads = [
        threading.Thread(target=_send, args=("create", "/v1/namespace/lance_catalog$p2$n/create")),
        threading.Thread(target=_send, args=("drop", "/v1/namespace/lance_catalog$p2/drop")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT)
    app.dependency_overrides[get_namespace] = lambda: ns
    parent = subtree.post("/v1/namespace/lance_catalog$p2/exists", json={}).status_code
    child = subtree.post("/v1/namespace/lance_catalog$p2$n/exists", json={}).status_code

    assert not (parent == 404 and child == 200), f"the create answered {answers.get('create')}, the drop {answers.get('drop')}, and p2$n stands under no p2"


def test_an_undrop_cut_short_is_finished_by_its_reissue(subtree: TestClient) -> None:
    # undrop_namespace clears the namespaces' records only once every table is registered (G3), so a register that
    # fails leaves the record of `p` for the re-issue, which finishes the job.
    from catalog.api.dependencies import get_namespace
    from catalog.main import app

    if (dropped := subtree.post(f"/v1/namespace/{P}/drop", json={"behavior": "Cascade"})).status_code != 200:
        pytest.fail(f"the cascade answered {dropped.status_code}: {dropped.text}")
    ns = cast(LanceNamespace, app.dependency_overrides[get_namespace]())
    # One connection for every request, so its register fails once in all, not once per request.
    failing = _FailsOnce(ns, "register_table", "t")
    app.dependency_overrides[get_namespace] = lambda: failing
    if (failed := subtree.post(f"/management/v1/namespace/{P}/undrop")).status_code == 200:
        pytest.fail(f"the undrop did not fail at the register of {T}: {failed.text}")
    reissued = subtree.post(f"/management/v1/namespace/{P}/undrop")

    assert (reissued.status_code, _rows(subtree)) == (200, 3), f"the re-issued undrop answered {reissued.status_code}: {reissued.text}"
