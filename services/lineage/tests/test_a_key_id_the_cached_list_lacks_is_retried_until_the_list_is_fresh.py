"""A key id the cached list lacks is retried until a list read since it was first met decides it, and a cached list expires.

[[LH-064]]. A rotation publishes the identity's new key first, so a lineage that cached the list a moment earlier has not seen it.
Refusing the first event signed with the new key would take and discard honest provenance on a stale cache, and reading the store
again for every unknown key id would hand a forger the store. So a delivery whose key id is missing from the cached list is asked for
again while no read has begun since the key id was FIRST met and the last read is younger than the refresh interval (a rate limit
delays a verdict and never decides one). Any read that begins after that first sighting decides it, whichever event caused the read,
so the lane's redelivery after its retry delay always ends in a verdict: a key id the list does not hold is refused, which is taken
and never recorded. The list is also
the trust anchor, so a cached copy is served only until its TTL: a key the store has since removed stops verifying once the copy
expires. The memory of first sightings is bounded: a key id pushed out of it is met afresh, which is one more retry cycle for its
event, while a key id still in it keeps its verdict. A read that fails is shared like one that succeeds: every verification that
began before it ended takes its failure, a retry, rather than reading again, which would cost N verifications queued behind a
hung store N reads of its timeout each, every one of them holding a worker; a verification that begins after it ended reads afresh.

Driven through lineage's own app and lifespan, each event delivered to its ingest lane, with the key reader's clock injected so the
interval, the TTL and the redelivery are crossed without waiting, and the bound shrunk to one key id so a second one pushes the first
out. One event is built per signing key, so delivering a key's event again is the bus's redelivery of it. A delivery's clock time is when its
verification began, so deliveries at one instant are verifications that overlap, and a store that hangs moves the clock on by the
read's timeout. The secret store is the suite's double, and the signatures are built by the root conftest's
`EventSigner` from the wire format alone.
"""

import json
from typing import Any, Final

import httpx
import pytest
from lineage_lane import Feed, started

from lakehouse.events.bus_delivery import Outcome, Settlement
from lakehouse.events.published_keys import KEY_REFRESH_INTERVAL_SECONDS, KEY_TTL_SECONDS
from lakehouse.governed.signing_key import KEY_READ_TIMEOUT_SECONDS
from lineage import main
from lineage.core.config import LineageSettings
from lineage.services.signature import store_published_keys
from tests.secret_store_double import SecretStoreDouble


SIGNER = "service-maintenance"

#: What the lane did with one delivery: asked for it again, took it refused with nothing recorded, or recorded it.
RETRIED, REFUSED, RECORDED = "retried", "refused", "recorded"

#: How long the lane waits before a delivery it asked for again comes back: the settings' own default, which no test here
#: overrides.
REDELIVERY_SECONDS = LineageSettings.model_fields["delivery_retry_seconds"].default

PRIMER = "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5e"
RUN_IDS: Final = frozendict(
    {
        "old": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5f",
        "new": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a60",
        "unpublished": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a61",
        "unpublished-too": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a62",
    }
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _event(run_id: str) -> dict[str, Any]:
    return {
        "eventType": "COMPLETE",
        "eventTime": "2026-10-02T12:00:00+00:00",
        "run": {"runId": run_id, "facets": {"rask_author": {"name": SIGNER, "sub": SIGNER}}},
        "job": {"namespace": "maintenance", "name": "compaction.acme-silver"},
        "outputs": [{"namespace": "silver", "name": "acme-silver$events"}],
        "producer": "https://github.com/AI-Riksarkivet/rask",
        "schemaURL": "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent",
    }


def _outcome(settled: Settlement, newly_recorded: bool) -> str:
    if settled.outcome is Outcome.AGAIN:
        return RETRIED
    if settled.outcome is Outcome.TAKEN:
        return RECORDED if newly_recorded else REFUSED
    return f"unexpected settlement {settled}"


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
    secret_store: SecretStoreDouble,
    published_now: str | None,
    deliveries: list[tuple[float, str, str]],
    reads_of_the_store: int,
) -> None:
    """``published_now`` is the list the store serves after the priming read, or None for a store that hangs for the read's
    whole timeout and then fails."""
    monkeypatch.setenv("RASK_EVENT_SIGNERS", json.dumps([SIGNER]))
    monkeypatch.setenv("RASK_EVENT_DELEGATORS", json.dumps([]))
    # One key id remembered: the second distinct unknown one pushes the first out. `raising=False` lets the test run against a reader
    # with no bound at all (the red check); if the patch ever fails to take effect the eviction case fails, so it cannot pass unnoticed.
    monkeypatch.setattr("lakehouse.events.published_keys.MAX_UNKNOWN_KEY_IDS", 1, raising=False)
    clock = _Clock()
    signers = {name: event_signer(SIGNER) for name in RUN_IDS}
    events = {name: signer.sign(_event(RUN_IDS[name])) for name, signer in signers.items()}
    published = secret_store.hold(f"signing-public-{SIGNER}", {"keys": signers["old"].public})
    with started(monkeypatch, Feed()) as lineage:
        main.app.state.published_keys = store_published_keys(main.app.state.secret_store, clock=clock)
        primed = lineage.deliver({"data": signers["old"].sign(_event(PRIMER))})
        assert (primed, published.call_count) == (Settlement(outcome=Outcome.TAKEN), 1), "the list was not cached by a verified event, so this tests nothing"
        if published_now is None:

            def _hangs_then_fails(request: httpx.Request) -> httpx.Response:
                clock.now += KEY_READ_TIMEOUT_SECONDS
                raise httpx.ReadTimeout("the store did not answer within the read's timeout", request=request)

            published.mock(side_effect=_hangs_then_fails)
        else:
            published.mock(return_value=secret_store.held({"keys": ",".join(signers[name].public for name in published_now.split(","))}))

        outcomes: list[str] = []
        for at, signed_with, _ in deliveries:
            clock.now = at
            recorded_before = len(lineage.feed.recorded)
            settled = lineage.deliver({"data": events[signed_with]})
            outcomes.append(_outcome(settled, len(lineage.feed.recorded) > recorded_before))

    assert outcomes == [expected for _, _, expected in deliveries], f"the lane settled {outcomes}"
    assert published.call_count == reads_of_the_store, f"the store was read {published.call_count} times"
