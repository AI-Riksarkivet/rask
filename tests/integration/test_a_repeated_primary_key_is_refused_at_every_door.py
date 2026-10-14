"""[[LH-243]] A write that would repeat a table's primary key is refused at every door that lands rows.

Lance declares a primary key in field metadata and enforces it on no write (measured on pylance 12.0.0:
create, append and an unmatched merge-insert all commit a repeated key), while the first merge whose
source repeats a key the target holds is refused `Ambiguous merge inserts are prohibited` from then on.
One repeated `id` therefore wedges every downstream full-sync merge. The doors hold the key instead:
within the rows a write carries, and against the rows the table holds when the write lands.

Driven through the real catalog app on a real pylance ``dir`` backend, through the doors that write rows
server-side: ``/create``, ``/insert`` and ``/merge_insert``. Two inserts racing one new key must land it once,
whether or not they name a run in their Context ([[LH-225]]).
A client-written commit is judged at ``version/create``
(``services/catalog/tests/test_a_stock_client_commits_every_operation_through_version_create.py``).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable

import httpx
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
TABLE = "lh243$t"
_SCHEMA = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata={"lance-schema:unenforced-primary-key": "true"}), pa.field("v", pa.string())])

type Act = Callable[[TestClient, str], list[httpx.Response]]


def _rows(*ids: int) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "v": [f"v{i}" for i in ids]}, schema=_SCHEMA)


def _ipc(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _create(client: TestClient, *ids: int, mode: str = "Create") -> httpx.Response:
    return client.post(f"/v1/table/{TABLE}/create", params={"mode": mode}, content=_ipc(_rows(*ids)), headers=ARROW)


def _insert(client: TestClient, *ids: int, context: dict[str, str] | None = None) -> httpx.Response:
    return client.post(f"/v1/table/{TABLE}/insert", content=_ipc(_rows(*ids)), headers={**ARROW, **(context or {})})


def _merge(client: TestClient, *ids: int) -> httpx.Response:
    params = {"on": "id", "when_matched_update_all": "true", "when_not_matched_insert_all": "true"}
    return client.post(f"/v1/table/{TABLE}/merge_insert", params=params, content=_ipc(_rows(*ids)), headers=ARROW)


def _two_inserts_of_one_new_key(client: TestClient, _location: str, context: dict[str, str] | None = None) -> list[httpx.Response]:
    start = threading.Barrier(2)
    answers: list[httpx.Response] = []

    def insert() -> None:
        start.wait()
        answers.append(_insert(client, 9, context=context))

    threads = [threading.Thread(target=insert) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return answers


@pytest.mark.parametrize(
    ("act", "seeded", "statuses", "ids"),
    [
        pytest.param(lambda c, _l: [_create(c, 5, 5)], False, [400], None, id="fresh-create-repeats-within"),
        pytest.param(lambda c, _l: [_create(c, 5, 5, mode="Overwrite")], True, [400], [1, 2], id="overwrite-repeats-within"),
        pytest.param(lambda c, _l: [_insert(c, 5, 5)], True, [400], [1, 2], id="insert-repeats-within"),
        pytest.param(lambda c, _l: [_insert(c, 2, 5)], True, [400], [1, 2], id="insert-repeats-a-held-key"),
        pytest.param(lambda c, _l: [_merge(c, 5, 5)], True, [400], [1, 2], id="merge-source-repeats-within"),
        pytest.param(_two_inserts_of_one_new_key, True, [200, 400], [1, 2, 9], id="racing-inserts-land-a-new-key-once"),
        # A run in the Context sends an insert through the stamped commit, which must keep the key check across the race.
        pytest.param(
            lambda c, location: _two_inserts_of_one_new_key(c, location, {"x-rask-run-id": "run-7"}),
            True,
            [200, 400],
            [1, 2, 9],
            id="racing-inserts-naming-a-run-land-a-new-key-once",
        ),
    ],
)
def test_a_write_repeating_the_primary_key_is_refused_and_the_table_keeps_each_key_once(
    real_ns_client: TestClient, act: Act, seeded: bool, statuses: list[int], ids: list[int] | None
) -> None:
    assert real_ns_client.post("/v1/namespace/lh243/create", json={}).status_code == 200
    location = ""
    if seeded:
        created = _create(real_ns_client, 1, 2)
        assert created.status_code == 200, created.text
        location = str(created.json()["location"])

    answers = act(real_ns_client, location)

    assert sorted(a.status_code for a in answers) == statuses, [a.text for a in answers]
    assert all(a.json()["code"] == 13 for a in answers if a.status_code == 400), [a.text for a in answers]
    if ids is None:
        assert real_ns_client.post(f"/v1/table/{TABLE}/exists").status_code == 404
    else:
        assert sorted(lance.dataset(location).to_table()["id"].to_pylist()) == ids
