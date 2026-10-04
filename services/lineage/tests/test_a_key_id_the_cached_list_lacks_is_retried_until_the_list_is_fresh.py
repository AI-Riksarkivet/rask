"""A key id the cached list lacks is retried until a list read since it was first met decides it, and a cached list expires.

[[LH-064]]. A rotation publishes the identity's new key first, so a lineage that cached the list a moment earlier has not seen it.
Refusing the first event signed with the new key would ack and discard honest provenance on a stale cache, and reading the store again
for every unknown key id would hand a forger the store. So a key id missing from the cached list is answered RETRY while no read has
begun since the key id was FIRST met and the last read is younger than the refresh interval (a rate limit delays a verdict and never
decides one). Any read that begins after that first sighting decides it, whichever event caused the read, so the sidecar's redelivery
120 s later always ends in a verdict: a key id the list does not hold is refused, which is acked and never recorded. The list is also
the trust anchor, so a cached copy is served only until its TTL: a key the store has since removed stops verifying once the copy
expires. The memory of first sightings is bounded: a key id pushed out of it is met afresh, which is one more retry cycle for its
event, while a key id still in it keeps its verdict. A read that fails is shared like one that succeeds: every verification that
began before it ended takes its failure, a retry, rather than reading again, which would cost N verifications queued behind a
hung store N reads of its timeout each, every one of them holding a worker; a verification that begins after it ended reads afresh.

Driven through the registered `/lineage-events` route, with the key reader's clock injected so the interval, the TTL and the
redelivery are crossed without waiting, and the bound shrunk to one key id so a second one pushes the first out. One event is built
per signing key, so delivering a key's event again is the sidecar's redelivery of it. A delivery's clock time is when its
verification began, so deliveries at one instant are verifications that overlap, and a store that hangs moves the clock on by the
read's timeout. The sidecar's secret API is stood in for by respx, and the signatures are built by the root conftest's
`EventSigner` from the wire format alone.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI
from starlette.testclient import TestClient

from lineage.services.signature import dapr_published_keys
from lineage_kit.keys import KEY_REFRESH_INTERVAL_SECONDS, KEY_TTL_SECONDS
from service_kit.governed.signing_key import KEY_READ_TIMEOUT_SECONDS


SIGNER = "service-maintenance"
STORE = "lance-secrets"
SECRETS = f"http://localhost:3500/v1.0/secrets/{STORE}"

#: What the door did with one delivery: asked for it again, acked it as refused with nothing recorded, or recorded it.
RETRIED, REFUSED, RECORDED = "retried", "refused", "recorded"

#: The chart's `pubsubDeliveryRetry` is a constant 120 s.
REDELIVERY_SECONDS = 120.0

PRIMER = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5e"
RUN_IDS = {
    "old": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5f",
    "new": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a60",
    "unpublished": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a61",
    "unpublished-too": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a62",
}


class _Feed:
    def __init__(self) -> None:
        self.recorded: list[str] = []

    async def ingest_event(self, event: Any) -> None:
        self.recorded.append(event.run_id)

    async def run_output_names(self, _run_id: str) -> list[str]:
        return []

    async def recorded_event(self, _run_id: str, _event_type: str | None) -> dict[str, Any] | None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _event(run_id: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": run_id, "facets": {"author": {"name": SIGNER, "sub": SIGNER}}},
        "job": {"namespace": "maintenance", "name": "compaction.acme-silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


def _outcome(answered: dict[str, Any], newly_recorded: bool) -> str:
    if answered == {"status": "RETRY"}:
        return RETRIED
    if answered == {"status": "SUCCESS"}:
        return RECORDED if newly_recorded else REFUSED
    return f"unexpected answer {answered}"


@respx.mock
@pytest.mark.parametrize(
    ("published_now", "deliveries", "reads_of_the_store"),
    [
        pytest.param(
            "new,old",
            [(KEY_REFRESH_INTERVAL_SECONDS / 2, "new", RETRIED)],
            1,
            id="inside-the-interval-a-new-key-is-retried-and-the-store-is-not-asked-again",
        ),
        pytest.param(
            "new,old",
            [((KEY_REFRESH_INTERVAL_SECONDS + KEY_TTL_SECONDS) / 2, "new", RECORDED)],
            2,
            id="past-the-interval-the-list-is-read-afresh-and-the-new-key-verifies",
        ),
        pytest.param("new", [(KEY_TTL_SECONDS + 10, "old", REFUSED)], 2, id="past-the-ttl-a-key-the-store-no-longer-lists-stops-verifying"),
        pytest.param(
            "old",
            [(10, "unpublished", RETRIED), (110, "old", RECORDED), (10 + REDELIVERY_SECONDS, "unpublished", REFUSED)],
            2,
            id="a-read-another-event-causes-before-the-redelivery-does-not-defer-its-verdict",
        ),
        pytest.param(
            "old",
            # A verified event re-reads the list at TTL + 10 s, so the two redeliveries 25 s and 26 s later fall inside the interval that
            # follows that read: the key id still remembered is judged on it, and the one pushed out is met afresh and asked for again.
            [
                (1, "unpublished", RETRIED),
                (2, "unpublished-too", RETRIED),
                (KEY_TTL_SECONDS + 10, "old", RECORDED),
                (KEY_TTL_SECONDS + 35, "unpublished-too", REFUSED),
                (KEY_TTL_SECONDS + 36, "unpublished", RETRIED),
            ],
            2,
            id="at-the-bound-the-key-id-met-least-recently-is-forgotten-and-met-afresh",
        ),
        pytest.param(
            None,
            # The stale list is read again at TTL + 10 s and the read fails at TTL + 12 s: the two deliveries that began at
            # TTL + 10 s, behind it, take its failure, and the one that begins a second after it ended reads afresh.
            [
                (KEY_TTL_SECONDS + 10, "old", RETRIED),
                (KEY_TTL_SECONDS + 10, "old", RETRIED),
                (KEY_TTL_SECONDS + 10, "old", RETRIED),
                (KEY_TTL_SECONDS + 10 + KEY_READ_TIMEOUT_SECONDS + 1, "old", RETRIED),
            ],
            3,
            id="verifications-that-began-during-a-failed-read-share-it-and-a-later-one-reads-afresh",
        ),
    ],
)
def test_an_unknown_key_id_is_retried_until_the_list_is_fresh_and_a_cached_list_expires(
    monkeypatch: pytest.MonkeyPatch,
    event_signer: Any,
    published_now: str | None,
    deliveries: list[tuple[float, str, str]],
    reads_of_the_store: int,
) -> None:
    """``published_now`` is the list the store serves after the priming read, or None for a store that hangs for the read's
    whole timeout and then fails."""
    for key, value in {
        "APP_API_TOKEN": "the-estate-app-token",
        "LINEAGE_DAPR_ENABLED": "true",
        "DAPR_HTTP_PORT": "3500",
        "LINEAGE_SIGNERS": json.dumps([SIGNER]),
        "LINEAGE_DELEGATORS": json.dumps([]),
    }.items():
        monkeypatch.setenv(key, value)
    # One key id remembered: the second distinct unknown one pushes the first out. `raising=False` lets the test run against a reader
    # with no bound at all (the red check); if the patch ever fails to take effect the eviction case fails, so it cannot pass unnoticed.
    monkeypatch.setattr("lineage_kit.keys.MAX_UNKNOWN_KEY_IDS", 1, raising=False)
    from lineage.api.dapr import register_dapr
    from lineage.core.config import get_settings
    from service_kit.lakehouse.ns_errors import install_problem_handlers

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    register_dapr(app)
    feed = _Feed()
    app.state.repository = feed
    clock = _Clock()
    app.state.published_keys = dapr_published_keys(STORE, clock=clock)
    signers = {name: event_signer(SIGNER) for name in RUN_IDS}
    events = {name: signer.sign(_event(RUN_IDS[name])) for name, signer in signers.items()}
    published = respx.get(f"{SECRETS}/signing-public-{SIGNER}").mock(return_value=httpx.Response(200, json={"keys": signers["old"].public}))
    client = TestClient(app)
    headers = {"dapr-api-token": "the-estate-app-token"}
    primed = client.post("/lineage-events", json={"data": signers["old"].sign(_event(PRIMER))}, headers=headers)
    assert (primed.json(), published.call_count) == ({"status": "SUCCESS"}, 1), "the list was not cached by a verified event, so this tests nothing"
    if published_now is None:

        def _hangs_then_fails(request: httpx.Request) -> httpx.Response:
            clock.now += KEY_READ_TIMEOUT_SECONDS
            raise httpx.ReadTimeout("the store did not answer within the read's timeout", request=request)

        published.mock(side_effect=_hangs_then_fails)
    else:
        published.mock(return_value=httpx.Response(200, json={"keys": ",".join(signers[name].public for name in published_now.split(","))}))

    outcomes: list[str] = []
    for at, signed_with, _ in deliveries:
        clock.now = at
        recorded_before = len(feed.recorded)
        answered = client.post("/lineage-events", json={"data": events[signed_with]}, headers=headers)
        outcomes.append(_outcome(answered.json(), len(feed.recorded) > recorded_before))

    assert outcomes == [expected for _, _, expected in deliveries], f"the door answered {outcomes}"
    assert published.call_count == reads_of_the_store, f"the store was read {published.call_count} times"
