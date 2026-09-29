"""`GET /queue` — the diagnostic that would have caught the stream outage.

On 2026-08-05 a NATS restart re-formed the JetStream metadata group from two fresh peers and EVERY
stream in the estate disappeared. `nats stream ls` answered "No Streams defined". Nothing detected
it: liveness stayed green (correctly — it probes the process), and the ingest plane's `ensure_stream`
quietly recreated INGEST on the next publish, so this plane looked fine while four other streams
stayed missing for four and a half hours.

These tests pin the two properties that make the endpoint useful rather than decorative: an ABSENT
stream is an ANSWER (not an error), and the endpoint never fails — an operator diagnosing a broken
queue must not be handed a failure instead of a diagnosis.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest
from fastapi.testclient import TestClient


if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js import JetStreamContext

    from ingest.queue import WorkQueue


def _queue_over(js: object) -> WorkQueue:
    """A `WorkQueue` whose only live part is the JetStream seam under test.

    Structural fake + `cast`, the same shape and for the same reason as `conftest.activity_ctx`:
    `_warn_if_retention_disagrees` reads `self._js.stream_info` and touches `self._nc` not at all,
    while a real `JetStreamContext` needs a live NATS connection. Widening `WorkQueue.__init__` to
    accept `None` would weaken a production signature to suit a test.
    """
    from ingest.queue import WorkQueue

    return WorkQueue(cast("NatsClient", None), cast("JetStreamContext", js))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # The code default is `/api/v1`; every deployment sets `/api`, and so does every other endpoint
    # test here. Pinned rather than inherited so the assertions read against the deployed path.
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    from ingest import create_app

    return TestClient(create_app())


def test_an_UNREACHABLE_queue_is_reported_not_raised(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint's whole job is to answer WHEN things are broken.

    A 500 here would make it useless for its own purpose: the operator asking why the queue is down
    gets a failure instead of the reason.
    """
    monkeypatch.setenv("RASK_NATS_URL", "nats://127.0.0.1:1")  # nothing listens

    res = client.get("/api/queue")

    assert res.status_code == 200, "the diagnostic must answer even when the thing it diagnoses is down"
    body = res.json()
    assert body["reachable"] is False
    assert body["detail"], "an unreachable queue must say WHY, not just report false"


# ── `stranded` — the question `consumers` gets mistaken for ──────────────────


def test_ZERO_consumers_is_the_IDLE_state_not_a_fault() -> None:
    """The misreading this field exists to end, and it happened TWICE in one day.

    The drain creates one durable per run (`ingest-<run_id>`, `queue.py:197`) and it goes away with
    the run, so between runs nothing is bound. An automated audit and then a human reader both saw
    `consumers: 0` on the live estate and reported "no worker deployment exists" — a diagnosis that
    sent the next hour looking for a missing Deployment that was never supposed to exist.

    A field can be factually correct and still be the wrong answer to the question people bring it.
    """
    from ingest.queue_health import QueueHealth

    idle = QueueHealth(reachable=True, stream_present=True, messages=0, consumers=0)

    assert idle.stranded is False, "an EMPTY queue with no consumer is a plane at rest"


# ── releasing what a dead run left queued ────────────────────────────────────


def test_the_release_NEVER_fails_a_run_even_with_no_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The property the first version of this claimed and did not have.

    `release_run` already swallowed its purge and delete failures, so the docstring said "best-effort
    by construction" — but `WorkQueue.connect()` sat OUTSIDE that guard, and an unreachable broker
    raised `NoServersError` straight out of the terminal activity. That fails a run which has already
    committed its data, for the sake of tidying up after it.

    Caught by an existing test, not by review: `test_run_chain`'s A6 chain went red the moment the
    call was wired in.
    """
    import asyncio

    from ingest.runtime import release_run_units

    monkeypatch.setenv("RASK_NATS_URL", "nats://127.0.0.1:1")  # nothing listens

    assert asyncio.run(release_run_units("run-42")) == 0, "an unreachable broker must report zero released, not raise"


# ── the stream this plane is written against vs the one it got ───────────────


def test_a_DISAGREEING_stream_warns_and_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    """Loud, never fatal.

    Raising here would take the entire ingest plane down on every cluster whose stream predates the
    fix — turning a correctness debt into an outage, which is the trade `/queue` already refuses when
    it declines to gate liveness on NATS. The requirement is only that it stops being SILENT.
    """
    import asyncio
    import logging

    from ingest.queue import STREAM

    class _Info:
        config = type("C", (), {"retention": "limits"})()

    class _JS:
        async def stream_info(self, _name: str) -> _Info:
            return _Info()

    queue = _queue_over(_JS())

    with caplog.at_level(logging.WARNING):
        asyncio.run(queue._warn_if_retention_disagrees())

    assert any(STREAM in r.message and "WORK_QUEUE" in r.message for r in caplog.records), "a stream with the wrong retention passed without a word"


def test_a_MATCHING_stream_says_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """A warning that fires on the correct configuration trains people to ignore it, which is how a
    real signal dies. NATS spells it `workqueue`; the check normalises rather than matching one
    spelling, because the wire form has varied across client versions."""
    import asyncio
    import logging

    class _Info:
        config = type("C", (), {"retention": "workqueue"})()

    class _JS:
        async def stream_info(self, _name: str) -> _Info:
            return _Info()

    with caplog.at_level(logging.WARNING):
        asyncio.run(_queue_over(_JS())._warn_if_retention_disagrees())

    assert not caplog.records, "a correctly configured stream produced a warning"
