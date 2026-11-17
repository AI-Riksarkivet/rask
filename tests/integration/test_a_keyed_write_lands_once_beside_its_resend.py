"""An append sent under an Idempotency-Key lands once, and its answers say so, while its first attempt still runs beside
the resend that took its key over (ADR 0240, `specs/catalog/CommitPath.tla`).

The request deadline answers a request 503 at 300 s and lets its door run on (`lakehouse.deadline`), and a key's claim
lapses after its lease, also 300 s (`lakehouse.idempotency.LEASE`): a resend can take the key over from an attempt that is
still running. The model's trace is driven step by step through the real catalog app on a real pylance namespace: the
first attempt is held after its claim and before its write, the resend runs whole under a lease driven to zero (the
lease having passed), and the first attempt is released. What a client then reads over HTTP is judged: the rows landed
once, each answer is true, and the key answers the resend's success from then on.

- ``no-key``, a table declaring no primary key (guard KeyRenews): the first attempt renews its claim right before its
  commit, finds the key the resend's, and answers 409 with nothing committed. Without the renewal it appended its rows a
  second time and answered 200.
- ``primary-key``, a table declaring one (guard RefusalHoldsKey): the first attempt's rows meet the key values the
  resend landed, and it answers 409, the key no longer its own. Without the guard it answered 400, "the table already
  holds a row with" its own key, for the write its key landed.

Each runs on the local root, and on a real S3 store where `make lifecycle-rustfs-check` names one (``race_ns_client``).
"""

from __future__ import annotations

import threading
from datetime import timedelta
from functools import partial
from typing import Any

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


TABLE = "lance_catalog$ns1$t"
#: How long one request waits for the one it is interleaved with: far above what either takes, so it only bounds a hang.
WAIT = 30
_KEY = {"lance-schema:unenforced-primary-key": "true"}


def _rows(ids: list[int], *, keyed: bool) -> bytes:
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata=_KEY if keyed else None)])
    return encode_arrow_stream(pa.table({"id": pa.array(ids, pa.int64())}, schema=schema))


@pytest.mark.parametrize("keyed", [pytest.param(False, id="no-key"), pytest.param(True, id="primary-key")])
def test_an_append_resent_under_its_key_while_its_first_attempt_runs_lands_once(
    race_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch, keyed: bool
) -> None:
    from catalog.api import idempotency as keyed_doors
    from catalog.services import dataplane
    from lakehouse import idempotency

    client = race_ns_client
    assert client.post("/v1/namespace/lance_catalog$ns1/create", json={}).status_code == 200
    created = client.post(f"/v1/table/{TABLE}/create", content=_rows([1, 2, 3], keyed=keyed), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert created.status_code == 200, created.text
    insert = f"/v1/table/{TABLE}/insert?mode=append"
    headers = {"Content-Type": ARROW_STREAM_MEDIA_TYPE, "Idempotency-Key": "0b9e6a2c-5d1f-4e3a-9c7b-2a4d6e8f1c30"}
    body = _rows([7, 8], keyed=keyed)

    arrived, release = threading.Event(), threading.Event()
    writes = dataplane.insert_into_table

    def held_once(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 — forwards the dataplane's own call
        if not arrived.is_set():
            arrived.set()
            if not release.wait(WAIT):
                raise TimeoutError("the first attempt was never released")
        return writes(*args, **kwargs)

    monkeypatch.setattr(dataplane, "insert_into_table", held_once)
    first: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: first.update(answer=client.post(insert, content=body, headers=headers)))
    thread.start()
    assert arrived.wait(WAIT), "the first attempt never reached its write"

    # The first attempt's lease has passed: its claim is taken over by the resend, which runs whole.
    with monkeypatch.context() as lapsed:
        lapsed.setattr(keyed_doors.idempotency, "claim", partial(idempotency.claim, lease=timedelta(0)))
        resent = client.post(insert, content=body, headers=headers)
    release.set()
    thread.join(WAIT)
    again = client.post(insert, content=body, headers=headers)

    assert (resent.status_code, resent.json()) == (200, {"num_inserted_rows": 2, "version": 2}), resent.text
    answered = first["answer"]
    assert (answered.status_code, answered.json().get("code")) == (409, 14), answered.text
    assert (again.status_code, again.json()) == (200, resent.json()), again.text
    described = client.post(f"/v1/table/{TABLE}/describe", json={"load_detailed_metadata": True})
    counted = client.post(f"/v1/table/{TABLE}/count_rows", json={})
    assert (described.json()["version"], counted.text) == (2, "5"), "the append landed twice"
