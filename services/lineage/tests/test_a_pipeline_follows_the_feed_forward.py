"""A pipeline follows committed writes in order: `GET /events?since=<seq>` pages FORWARD, filtered and governed (LH-472).

Driven through lineage's real router behind its real authentication-then-FGA gate, as a bring-your-own
pipeline's service principal. What stands in, and why each may:

* the repository, as an in-memory feed: the feed table is Postgres, which no offline lane runs. The forward
  SQL and the commit-order guarantee it rests on are `tests/e2e-py/test_lineage_e2e.py`'s, against real
  Postgres;
* the verified caller, as lineage's `authenticate` overridden to the pipeline's principal;
* OpenFGA, as a client answering from the relations in `GRANTED`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openfga_sdk import OpenFgaClient
from openfga_sdk.client.models import ClientBatchCheckRequest, ClientCheckRequest, ClientTuple
from openfga_sdk.client.models.batch_check_response import ClientBatchCheckResponse
from openfga_sdk.client.models.batch_check_single_response import ClientBatchCheckSingleResponse
from openfga_sdk.models.check_response import CheckResponse

from lineage.api.security import authenticate
from lineage.api.v1.router import api_router
from lineage.core.config import get_settings
from lineage.schemas import EventRecord
from service_kit.governed.machine_identity import ServicePrincipal
from service_kit.lakehouse.ns_errors import install_problem_handlers


PIPELINE = ServicePrincipal(service_account="system:serviceaccount:etl:etl-runner")

#: The tables the pipeline may see. `acme-silver$hidden` and `globex-silver$orders` are someone else's.
GRANTED = frozenset(
    ("user:kubernetes~system%3Aserviceaccount%3Aetl%3Aetl-runner", "can_get_metadata", f"table:{name}")
    for name in ("acme-silver$pages", "acme-gold$catalog", "acme-gold$index")
)

#: The feed as committed. Seq numbers have holes, as a real feed does: an insert that loses its
#: `ON CONFLICT` still draws a seq.
FEED = [
    EventRecord(seq=1, event={}, outputs=["acme-silver$pages"]),
    EventRecord(seq=2, event={}, outputs=["acme-silver$hidden"]),
    EventRecord(seq=3, event={}, inputs=["acme-silver$pages"], outputs=["acme-gold$catalog"]),
    EventRecord(seq=5, event={}, outputs=["globex-silver$orders"]),
    EventRecord(seq=8, event={}, outputs=["acme-silver$pages"]),
    EventRecord(seq=9, event={}, outputs=["acme-gold$index"]),
    EventRecord(seq=12, event={}, inputs=["acme-silver$hidden"], outputs=["acme-gold$catalog"]),
]


class _Feed:
    """The repository's feed contract in memory: `since` pages forward (`seq > since`, oldest first)."""

    async def list_events(self, limit: int = 500, *, after: int | None = None, since: int | None = None, summary: bool = False) -> list[EventRecord]:
        del summary
        if since is not None:
            return sorted((row for row in FEED if row.seq > since), key=lambda row: row.seq)[:limit]
        return sorted((row for row in FEED if after is None or row.seq < after), key=lambda row: row.seq, reverse=True)[:limit]

    async def oldest_event_seq(self) -> int | None:
        return min(row.seq for row in FEED)


class _OpenFga:
    """Check and BatchCheck answered from `GRANTED`, through the SDK's own request and response classes."""

    async def check(self, body: ClientCheckRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> CheckResponse:
        del options
        return CheckResponse(allowed=(body.user, body.relation, body.object) in GRANTED)

    async def batch_check(self, body: ClientBatchCheckRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> ClientBatchCheckResponse:
        del options
        return ClientBatchCheckResponse(
            [
                ClientBatchCheckSingleResponse(
                    allowed=(item.user, item.relation, item.object) in GRANTED, request=cast("ClientTuple", item), correlation_id=f"c{index}", error=None
                )
                for index, item in enumerate(body.checks)
            ]
        )


@pytest.fixture
def feed(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_FGA_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ENABLED", "true")
    monkeypatch.setenv("RASK_OIDC_ISSUER", "https://idp.invalid/dex")
    monkeypatch.setenv("RASK_OIDC_AUDIENCE", "rask")
    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    app.include_router(api_router)
    app.dependency_overrides[authenticate] = lambda: PIPELINE
    app.state.fga = cast(OpenFgaClient, _OpenFga())
    app.state.repository = _Feed()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def _follow(client: TestClient, **filters: str) -> list[int]:
    """Walk the feed the way a pipeline does: poll from the last cursor until a poll brings nothing new."""
    seen: list[int] = []
    cursor = 0
    for _ in range(20):
        response = client.get("/events", params={"since": cursor, "limit": 2, **filters})
        assert response.status_code == 200, response.text
        page = response.json()
        seen += [event["seq"] for event in page["events"]]
        if not page["events"] and page["next_cursor"] == cursor:
            return seen
        assert page["next_cursor"] > cursor, f"the cursor must move forward or the walk is over: {page}"
        cursor = page["next_cursor"]
    raise AssertionError(f"the walk did not settle: {seen}")


def test_since_pages_forward_oldest_first_with_no_gap_or_repeat(feed: TestClient) -> None:
    assert _follow(feed) == [1, 3, 8, 9]


def test_a_table_or_namespace_filter_narrows_the_feed_and_hidden_events_stay_hidden(feed: TestClient) -> None:
    assert _follow(feed, table="acme-silver$pages") == [1, 3, 8]
    assert _follow(feed, namespace="acme-gold") == [3, 9]
    assert _follow(feed, table="acme-silver$hidden") == []
