"""A parked delivery is re-presented to the INGEST path before it is written off as loss.

[[LH-148]], and the gap is stated in `on_dead_letter`'s own docstring: *"a dead letter older than the
stream's retention has no path back, because nothing re-ingests the DLQ stream itself."* That sentence
was the whole defect. The parking route receives the payload, logs it, counts it and ACKS it — and the
event then sits on a stream with `Max Age 7d` and `Discard Old` until it ages out.

THE LOSS IS UNDER WAY, not hypothetical. Measured on the live estate 2026-09-22 minutes apart:
`nats stream info DLQ` reported Messages 1,659 then 1,585, First Sequence 10,640 then 10,714 — 74
parked deliveries aged out of the window while the audit that found this was still running.

WHY RE-PRESENTING IS SOUND RATHER THAN A RETRY LOOP. `handle_cloud_event` is the same function the live
subscription uses, so a replay carries the SAME authorization (`enforce_bus_authz` — may the stamped
subject record THIS) and the same idempotence (`ingest_event` MERGEs on a deterministic run id). It
writes to the repository directly and publishes nothing, so a recovered event never lands back on
`lineage.events.v1` — which is the closes-when's first clause, and the reason this cannot be done
through the reconcile relay, whose drain re-publishes by design.

IT DOES NOT PRE-EMPT THE OWNER'S RULING on the ~86% role-literal population (`author.sub` = a role, not
a person). Those fail `enforce_bus_authz` by construction, so they park exactly as before and their
disposition stays open. What changes is only the authorizable remainder, which today is lost on a timer.

THE OUTCOME IS MEASURED, NOT ASSUMED. `handle_cloud_event` answers SUCCESS both for a committed write
and for an unrepairable discard, so its status cannot tell recovery from a shrug. The route asks the
graph again instead: a run the graph lacked and now holds was RECOVERED.

`DEAD_LETTERED` MEANS THE GRAPH IS MISSING THE RUN, which is the claim an alert on it makes. Measured
against the live estate 2026-09-13: the DLQ held 8,515 parked deliveries of `lineage.events.v1` while the
stream's last sequence was 5,860, and 17 of 21 distinct parked run ids sampled were already in the AGE
graph. Run ids are deterministic (UUIDv5) and `ingest_event` MERGEs on `run_id`, so every later park of a
run the graph holds is visibility, not loss, and counting it as loss inflates the signal by roughly two
orders of magnitude. The route asks `run_status` first. Every fallback leans toward loss: no run id, no
repository, or a repository that raises all record `DEAD_LETTERED`, because a parking route that cannot
ask must not answer "nothing was lost".
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.ns_errors import install_problem_handlers


class _Repo:
    """`run_status` + `ingest_event` — the two the parking route needs.

    `ingest_event` MOVES the run into `known`, which is what lets the route's second `run_status` read
    report recovery honestly rather than trusting an ack status. `raises` makes `run_status` fail, the
    unreachable-graph case.
    """

    def __init__(self, *, known: set[str] | None = None, accepts: bool = True, raises: bool = False) -> None:
        self.known = known or set()
        self.accepts = accepts
        self.raises = raises
        self.ingested: list[str] = []
        self.asked: list[str] = []

    async def run_status(self, run_id: str) -> object | None:
        self.asked.append(run_id)
        if self.raises:
            raise RuntimeError("graph unreachable")
        return object() if run_id in self.known else None

    async def ingest_event(self, event: Any) -> None:
        run_id = event.run.run_id
        self.ingested.append(run_id)
        if not self.accepts:
            raise RuntimeError("graph write failed")
        self.known.add(run_id)


def _client(monkeypatch: pytest.MonkeyPatch, repo: _Repo | None, *, authorized: bool = True) -> TestClient:
    """The parking route on its own app. ``repo=None`` leaves ``app.state`` without a repository."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("LINEAGE_DLQ_TOPIC", "dlq.lineage.events")

    import lineage.api.dapr as dapr_mod
    from lineage.core.config import get_settings

    get_settings.cache_clear()

    # THE WHOLE SIGNATURE, including `arrived` — the door verifies a producer signature against the
    # bytes that reached it, so the bytes are an argument. A three-parameter double raises TypeError
    # inside the route's `except Exception`, which reads as an authz OUTAGE and answers RETRY: the
    # replay then looks like an unreachable authorizer rather than a broken stand-in.
    async def _authz(parsed: Any, request: Any, settings: Any, arrived: Any) -> None:
        if not authorized:
            from lineage.api.fga_deps import UnauthoredRunError

            raise UnauthoredRunError("the stamped subject is a role literal, not a person")

    monkeypatch.setattr(dapr_mod, "enforce_bus_authz", _authz)
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    dapr_mod.register_dapr(app)
    if repo is not None:
        app.state.repository = repo
    get_settings.cache_clear()
    return TestClient(app)


def _event(run_id: str | None) -> dict[str, Any]:
    """A parseable OpenLineage event. ``run_id=None`` omits ``runId``: a park with no readable run."""
    run: dict[str, Any] = {"facets": {"author": {"sub": "alice"}}}
    if run_id is not None:
        run["runId"] = run_id
    return {
        "id": "evt-1",
        "data": {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-22T12:00:00Z",
            "producer": "https://rask/test",
            "job": {"namespace": "rask", "name": "probe"},
            "run": run,
            "inputs": [],
            "outputs": [],
        },
    }


def _park(client: TestClient, payload: dict[str, Any]) -> dict[str, str]:
    response = client.post("/lineage-dlq", json=payload, headers={"dapr-api-token": "s3cret"})
    assert response.status_code == 200
    return response.json()


def _outcomes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import lineage.api.dapr as dapr_mod

    seen: list[str] = []
    # `door` is keyword-only on `record_outcome`, so a lambda that omits it cannot be CALLED — which is
    # the point of it having no default: a double must stand for the whole signature.
    monkeypatch.setattr(dapr_mod, "record_outcome", lambda outcome, *, door: seen.append(str(outcome)))
    return seen


def test_an_authorizable_parked_delivery_is_re_ingested_and_counted_as_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The closes-when's first clause: a parked delivery reaches the graph again."""
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known=set())
    client = _client(monkeypatch, repo)

    assert _park(client, _event("run-lost")) == {"status": "SUCCESS"}
    assert repo.ingested == ["run-lost"], "the parking route never re-presented the payload to the ingest path"
    assert "run-lost" in repo.known, "the graph still lacks the run the route claims to have recovered"
    assert seen == [str(Outcome.RECOVERED)], f"a recovered run is not terminal loss, got {seen}"


def test_a_role_literal_delivery_still_parks_and_the_ruling_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ~86% the owner has not ruled on must behave EXACTLY as before — refused, parked, counted."""
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known=set())
    client = _client(monkeypatch, repo, authorized=False)

    assert _park(client, _event("run-role-literal")) == {"status": "SUCCESS"}
    assert repo.ingested == [], "an unauthorized delivery must never reach the graph"
    assert seen == [str(Outcome.DEAD_LETTERED)], f"an unrecoverable park is still the loss signal, got {seen}"


def test_a_failed_re_ingest_falls_back_to_exactly_the_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never worse than before: if the replay raises, the route parks and acks as it always did."""
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known=set(), accepts=False)
    client = _client(monkeypatch, repo)

    assert _park(client, _event("run-broken")) == {"status": "SUCCESS"}, "a failed replay must still ACK, never spin the DLQ"
    assert seen == [str(Outcome.DEAD_LETTERED)]


def test_a_park_whose_run_the_graph_already_holds_is_not_counted_as_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known={"run-abc"})
    client = _client(monkeypatch, repo)

    assert _park(client, _event("run-abc")) == {"status": "SUCCESS"}  # still ACKs — never a retry loop
    assert repo.asked == ["run-abc"], "the parking route must ask the graph before calling it loss"
    assert seen == [str(Outcome.PARKED_ALREADY_RECORDED)], f"a re-park of a recorded run is not terminal loss, got {seen}"


def test_a_recorded_park_logs_at_warning_and_a_lost_one_at_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The two answers send an operator to different places, so they must not share a severity: an
    ERROR per re-park is what let the real losses scroll away among them."""
    client = _client(monkeypatch, _Repo(known={"run-abc"}))
    with caplog.at_level(logging.WARNING, logger="lineage.api.dapr"):
        _park(client, _event("run-abc"))
    recorded = next(r for r in caplog.records if r.message == "dapr_dead_letter_parked")
    assert recorded.levelno == logging.WARNING
    assert getattr(recorded, "already_recorded", None) is True
    assert getattr(recorded, "author", None) == "alice", "the author must survive the new branch"

    caplog.clear()
    client_missing = _client(monkeypatch, _Repo(known=set(), accepts=False))
    with caplog.at_level(logging.WARNING, logger="lineage.api.dapr"):
        _park(client_missing, _event("run-gone"))
    lost = next(r for r in caplog.records if r.message == "dapr_dead_letter_parked")
    assert lost.levelno == logging.ERROR
    assert getattr(lost, "already_recorded", None) is False


@pytest.mark.parametrize(
    "repo",
    [pytest.param(None, id="no-repository"), pytest.param(_Repo(raises=True), id="graph-unreachable")],
)
def test_a_parking_route_that_cannot_ask_reports_loss(monkeypatch: pytest.MonkeyPatch, repo: _Repo | None) -> None:
    """Both fallbacks lean toward loss. Answering "already recorded" when the graph could not be
    consulted would silence the counter exactly when it matters most."""
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    client = _client(monkeypatch, repo)
    assert _park(client, _event("run-abc")) == {"status": "SUCCESS"}
    assert seen == [str(Outcome.DEAD_LETTERED)], f"an unanswerable question is not an answer of 'no loss', got {seen}"


def test_a_park_carrying_no_readable_run_id_reports_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known={"run-abc"})
    client = _client(monkeypatch, repo)
    assert _park(client, _event(None)) == {"status": "SUCCESS"}
    assert repo.asked == [], "with no run id there is nothing to ask the graph"
    assert seen == [str(Outcome.DEAD_LETTERED)]


def test_the_run_id_is_read_off_a_payload_that_will_not_parse() -> None:
    """Tolerant for the same reason `author_sub_from_payload` is: this fires on the discard path, where
    the strict model is unavailable exactly where the answer is needed."""
    from lineage.models import run_id_from_payload

    assert run_id_from_payload({"run": {"runId": "r-1"}}) == "r-1"
    assert run_id_from_payload({"run": {"runId": "  "}}) is None  # blank names no run
    for junk in ({}, {"run": {}}, {"run": None}, {"run": {"runId": 7}}, "not-a-dict", None):
        assert run_id_from_payload(junk) is None
