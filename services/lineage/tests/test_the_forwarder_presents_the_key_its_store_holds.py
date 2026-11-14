"""The forwarder presents the graph backend the API key lineage's secret store holds, and sends nothing while it holds none.

A backend that asks for a key (`services.lineage.forward.authSecret`) gets it as a bearer, read from the secret store
under lineage's own login on the tick that sends, so a rotated key needs no restart. A store that holds no key fails
the tick closed: the event waits in the feed with the cursor where it was, and is never sent without the credential the
deployment named. So does a key no HTTP header carries: the client refuses to send one and quotes it in its error, which
the tick would log, so it is refused before it is sent and named by nothing.

Driven through `forward`, the relay tick's own call, against the secret store's double and a backend respx answers. The
feed is an in-memory one of this test's own: the real one is a catalog's tables, and a catalog in this process would
share the process's one secret-store session.
"""

from __future__ import annotations

from typing import cast

import httpx
import pytest
import respx

from lineage.core.config import LineageSettings
from lineage.schemas import EventRecord
from lineage.services.forward import forward
from lineage.services.provenance import FeedWindow, ProvenanceStore
from tests.secret_store_double import SecretStoreDouble


BACKEND = "http://graph-backend.test:5000"
EVENT = {"eventType": "COMPLETE", "run": {"runId": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5e"}}


class _Window:
    """One read of an in-memory feed, with `FeedWindow`'s signatures."""

    def __init__(self, rows: list[EventRecord]) -> None:
        self._rows = list(rows)

    async def bounds(self) -> tuple[int, int] | None:
        return (self._rows[0].seq, self._rows[-1].seq) if self._rows else None

    async def take(self, n: int) -> list[EventRecord]:
        taken, self._rows = self._rows[:n], self._rows[n:]
        return taken


class _Feed:
    """An in-memory feed holding one event and the forwarder's cursor, with `ProvenanceStore`'s signatures."""

    def __init__(self) -> None:
        self.cursors: dict[str, int] = {}

    async def cursor(self, follower: str) -> int:
        return self.cursors.get(follower, 0)

    async def window(self, *, after: int | None = None, since: int | None = None, summary: bool = False) -> FeedWindow:
        return cast("FeedWindow", _Window([EventRecord(seq=1, event=EVENT)] if (since or 0) < 1 else []))

    async def advance_cursor(self, follower: str, seq: int) -> None:
        self.cursors[follower] = seq


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("held", "sent"),
    [
        pytest.param({"api-key": "the-backends-key\n"}, ["Bearer the-backends-key"], id="the-key-the-store-holds-less-the-newline-it-was-seeded-with"),
        pytest.param({"another-field": "not-the-key"}, [], id="a-store-that-holds-no-key"),
        pytest.param({"api-key": "the backends\tkey"}, [], id="a-key-no-header-carries"),
    ],
)
@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_the_forwarder_sends_an_event_only_with_the_key_its_store_holds(
    respx_mock: respx.MockRouter, secret_store: SecretStoreDouble, held: dict[str, str], sent: list[str]
) -> None:
    took = respx_mock.post("/api/v1/lineage").mock(return_value=httpx.Response(201))
    key = secret_store.hold("graph-backend", held)
    settings = LineageSettings.model_validate({"forward_url": BACKEND, "forward_api_key_secret": "graph-backend"})
    feed = _Feed()

    async with httpx.AsyncClient() as client:
        if sent:
            outcome = await forward(cast("ProvenanceStore", feed), settings, client)
            assert outcome.forwarded == 1
        else:
            with pytest.raises(RuntimeError, match="failing closed") as refused:
                await forward(cast("ProvenanceStore", feed), settings, client)
            assert [value for value in held.values() if value in str(refused.value)] == []

    assert ([call.request.headers["Authorization"] for call in took.calls], key.call_count, feed.cursors) == (
        sent,
        1,
        dict.fromkeys(feed.cursors, 1) if sent else {},
    )
