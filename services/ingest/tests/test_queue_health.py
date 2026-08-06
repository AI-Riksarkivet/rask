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

import pathlib

import pytest
from fastapi.testclient import TestClient


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


def test_reachability_and_stream_PRESENCE_are_separate_fields(client: TestClient) -> None:
    """The distinction is the entire lesson of the outage.

    "NATS is down" and "NATS is up but the stream this plane needs does not exist" are different
    incidents with different fixes — and the second is the one that hid, because every liveness and
    readiness signal in the estate stayed green throughout.
    """
    res = client.get("/api/queue")

    assert res.status_code == 200
    body = res.json()
    assert "reachable" in body
    assert "stream_present" in body
    assert "dlq_present" in body


def test_the_DLQ_is_reported_because_its_absence_is_SILENT(client: TestClient) -> None:
    """The quietest failure in the plane, and the reason the DLQ gets its own field.

    `park_poison` publishes and moves on. With no DLQ stream that publish fails and a corrupt unit is
    DROPPED rather than parked — while the run still reports the error, so the operator sees a normal
    validation failure and never learns the evidence was thrown away. Nothing else in the estate
    would ever surface it.
    """
    res = client.get("/api/queue")

    assert "dlq_present" in res.json(), "the DLQ's presence must be reported separately from the work stream's"


def test_the_diagnostic_is_NOT_folded_into_liveness() -> None:
    """Liveness must stay dumb, and this is a structural assertion rather than a style preference.

    A liveness probe that fails when NATS is down turns ONE broken dependency into a restart loop
    across every pod that touches it — a partial outage becomes a total one. `health.py` says so in
    its own docstring; this keeps the two from being merged by someone who reasonably thinks a
    health endpoint should check health.
    """
    import ast

    import ingest.health as health

    tree = ast.parse(pathlib.Path(health.__file__).read_text(encoding="utf-8"))

    # AST, not a text scan: `health.py`'s own docstring EXPLAINS that it deliberately does not probe
    # NATS, so a substring check trips on the sentence justifying the design — and the tempting fix is
    # to delete the explanation. Same trap the `signal_drained` gate hit.
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        (node.module or "").split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }

    assert "nats" not in imported, "the liveness route imported a NATS client — one dependency outage would now restart every pod"
    assert not any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("stream_info", "jetstream", "connect") for n in ast.walk(tree)
    ), "the liveness route grew a dependency probe"


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


def test_messages_with_NO_consumer_is_work_nobody_is_coming_for() -> None:
    """The real defect, and the one combination that means work is LOST.

    WORK_QUEUE retention removes a message only when it is ACKED. A unit whose run died takes its
    durable consumer with it, and the message then sits forever: no consumer is ever created for that
    run id again, nothing sweeps the stream, and every other signal stays green. Measured on the live
    estate — one message, no consumers, unchanged across an hour of polling.
    """
    from ingest.queue_health import QueueHealth

    stuck = QueueHealth(reachable=True, stream_present=True, messages=1, consumers=0)

    assert stuck.stranded is True


def test_a_DRAINING_run_is_not_stranded() -> None:
    """Messages plus a bound consumer is the plane WORKING. Reporting that as stranded would train
    an operator to ignore the field, which is how a real signal dies."""
    from ingest.queue_health import QueueHealth

    busy = QueueHealth(reachable=True, stream_present=True, messages=500, consumers=1)

    assert busy.stranded is False


def test_an_UNREACHABLE_queue_says_UNKNOWN_rather_than_fine() -> None:
    """`None`, not `False`. The two are different claims and only one of them is true.

    `False` asserts "nothing is stranded" — from a probe that could not look. That is the same
    false-negative shape as A8 certifying provenance it could not read: an unanswerable question
    reported as a clean answer.
    """
    from ingest.queue_health import QueueHealth

    down = QueueHealth(reachable=False, detail="connection refused")

    assert down.stranded is None


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


def test_the_release_is_BOUNDED_so_a_terminal_run_cannot_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    """A terminal run must not wait on a broker to finish terminating.

    Measured in this plane: a nats connect to a dead address had still not returned after 60 seconds
    with `connect_timeout`, `allow_reconnect=False` and `max_reconnect_attempts=0` ALL set — the
    client does not honour its own deadline, so `asyncio.wait_for` around the whole thing is the only
    reliable bound. Wiring this call in without one took the ingest suite from 21s to 150s, which is
    the cheap version of the same failure: in production it is a run that never reports.
    """
    import asyncio
    import time

    from ingest.runtime import RELEASE_TIMEOUT_SECONDS, release_run_units

    monkeypatch.setenv("RASK_NATS_URL", "nats://127.0.0.1:1")

    started = time.monotonic()
    asyncio.run(release_run_units("run-42"))
    elapsed = time.monotonic() - started

    assert elapsed < RELEASE_TIMEOUT_SECONDS * 3, f"the release took {elapsed:.1f}s — the deadline is not bounding the connect"


def test_the_release_runs_on_the_TERMINAL_path_not_in_a_sweep() -> None:
    """Structural, and it is the design rather than a detail.

    A sweep would have to re-derive "this run has no live workflow" from outside — the same inference
    `/queue`'s `stranded` flag exists because two readers got it wrong on the same day. The workflow
    does not infer: `emit_terminal` KNOWS the run is ending, so it releases what the run published.

    Move this into a cron or a reconciler and the inference comes back.
    """
    import inspect

    from ingest.workflow import emit_terminal

    assert "release_run_units" in inspect.getsource(emit_terminal), (
        "the terminal activity stopped releasing its queued units — a run that dies between "
        "publish_units and drain_chunk now strands them behind a consumer that was never created"
    )
