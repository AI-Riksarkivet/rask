"""Of a stock client's create and a catalog create of one name, at most one answers success, and it is the one whose rows
the table holds (ADR 0227 §9, the open finding StockCreateRace, `specs/catalog/TableLifecycleStockCreateRace.cfg`).

A stock client creates a table by declaring it (`DeclareTable`, whose claim takes no fence), writing its rows with its own
Lance on the credential the catalog vends, and finalizing version 1 through `version/create` (ADR 0157). A catalog create
of the name meanwhile finds the declaration empty, fences it and writes there; the client's version 1 lands between the
create's probe and its commit, and the create's commit, which Lance does not condition on its check (A4), lands over the
client's rows as version 2. Both answer success. Closing it needs a design decision ADR 0227 §9 states the alternatives
of, so the test stands strict-xfail until it is taken.

Driven over a real socket, as `test_a_create_that_commits_rows_is_announced_as_a_write.py` drives a stock client: the
real catalog app on a real `dir` namespace, stock pylance as the client.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest

from tests.integration.lineage_app import live_catalog


NAMESPACE = "lance_catalog$ns1"
TABLE = f"{NAMESPACE}$t"
#: How long one request waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 60


class _Gate:
    def __init__(self) -> None:
        self.arrived, self.release = threading.Event(), threading.Event()

    def hold(self) -> None:
        self.arrived.set()
        if not self.release.wait(WAIT):
            raise TimeoutError("the held step was never released")


def _held_once(gate: _Gate, real: Callable[..., Any], *, after: bool = False) -> Callable[..., Any]:
    """``real``, its first call held at ``gate`` before it runs, or ``after`` it has run and before it answers."""
    pending = [True]

    def _call(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — the held function's own arguments
        first = bool(pending)
        pending.clear()
        if first and not after:
            gate.hold()
        answer = real(*args, **kwargs)
        if first and after:
            gate.hold()
        return answer

    return _call


def _arrow(ids: list[int]) -> bytes:
    rows = pa.table({"id": pa.array(ids, pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, rows.schema) as writer:
        writer.write_table(rows)
    return sink.getvalue().to_pybytes()


@pytest.mark.xfail(strict=True, reason="ADR 0227 §9: StockCreateRace is open until its design decision is taken")
def test_of_a_stock_create_and_a_catalog_create_of_one_name_only_the_one_whose_rows_the_table_holds_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from catalog.services import dataplane, table_claims

    declares, commits = _Gate(), _Gate()
    monkeypatch.setattr(dataplane, "declare_table", _held_once(declares, dataplane.declare_table, after=True))
    monkeypatch.setattr(table_claims, "renew_fence", _held_once(commits, table_claims.renew_fence))
    answered: dict[str, bool] = {}
    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        assert httpx.post(f"{url}/v1/namespace/{NAMESPACE}/create", json={}, timeout=WAIT).status_code == 200

        def _stock() -> None:
            try:
                lance.write_dataset(
                    pa.table({"id": pa.array([1, 2, 3], pa.int64())}),
                    namespace_client=lance_namespace.connect("rest", {"uri": url}),
                    table_id=[str(segment) for segment in TABLE.split("$")],
                    mode="create",
                    data_storage_version="2.2",
                    enable_stable_row_ids=True,
                )
            except Exception:  # noqa: BLE001 — a refused create is an answer, the claim judges it
                answered["stock"] = False
                return
            answered["stock"] = True

        def _catalog() -> None:
            created = httpx.post(
                f"{url}/v1/table/{TABLE}/create", content=_arrow([7, 8]), headers={"content-type": "application/vnd.apache.arrow.stream"}, timeout=WAIT
            )
            answered["catalog"] = created.status_code == 200

        # The client's declare has committed and is held before it answers; the catalog create finds the declaration
        # empty, fences it and writes, and is held right before its commit; the client then writes and finalizes version 1.
        stock = threading.Thread(target=_stock)
        stock.start()
        declares.arrived.wait(WAIT)
        catalog = threading.Thread(target=_catalog)
        catalog.start()
        commits.arrived.wait(WAIT)
        declares.release.set()
        stock.join(WAIT)
        commits.release.set()
        catalog.join(WAIT)
        counted = httpx.post(f"{url}/v1/table/{TABLE}/count_rows", json={}, timeout=WAIT)
        held = int(counted.text) if counted.status_code == 200 else None

    succeeded = [rows for rows, door in ((3, "stock"), (2, "catalog")) if answered.get(door)]
    assert succeeded == [held], f"the stock create and the catalog create answered {answered}, and the table holds {held} rows"
