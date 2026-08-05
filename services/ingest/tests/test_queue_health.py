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
