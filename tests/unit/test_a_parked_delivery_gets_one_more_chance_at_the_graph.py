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
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.ns_errors import install_problem_handlers


class _Repo:
    """`run_status` + `ingest_event` — the two the parking route now needs.

    `ingest_event` MOVES the run into `known`, which is what lets the route's second `run_status` read
    report recovery honestly rather than trusting an ack status.
    """

    def __init__(self, *, known: set[str] | None = None, accepts: bool = True) -> None:
        self.known = known or set()
        self.accepts = accepts
        self.ingested: list[str] = []
        self.asked: list[str] = []

    async def run_status(self, run_id: str) -> object | None:
        self.asked.append(run_id)
        return object() if run_id in self.known else None

    async def ingest_event(self, event: Any) -> None:
        run_id = event.run.run_id
        self.ingested.append(run_id)
        if not self.accepts:
            raise RuntimeError("graph write failed")
        self.known.add(run_id)


def _client(monkeypatch: pytest.MonkeyPatch, repo: _Repo, *, authorized: bool = True) -> TestClient:
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
    app.state.repository = repo
    get_settings.cache_clear()
    return TestClient(app)


def _event(run_id: str) -> dict[str, Any]:
    return {
        "id": "evt-1",
        "data": {
            "eventType": "COMPLETE",
            "eventTime": "2026-09-22T12:00:00Z",
            "producer": "https://rask/test",
            "job": {"namespace": "rask", "name": "probe"},
            "run": {"runId": run_id, "facets": {"author": {"sub": "alice"}}},
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


def test_a_run_the_graph_already_holds_is_not_re_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control, and it is a cost argument: re-presenting a run already in the graph buys nothing and
    spends a write on every restart, because the ingest consumer re-reads the retained stream."""
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known={"run-known"})
    client = _client(monkeypatch, repo)

    assert _park(client, _event("run-known")) == {"status": "SUCCESS"}
    assert repo.ingested == [], "a run the graph already holds must not be written again"
    assert seen == [str(Outcome.PARKED_ALREADY_RECORDED)]


def test_a_failed_re_ingest_falls_back_to_exactly_the_old_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never worse than before: if the replay raises, the route parks and acks as it always did."""
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known=set(), accepts=False)
    client = _client(monkeypatch, repo)

    assert _park(client, _event("run-broken")) == {"status": "SUCCESS"}, "a failed replay must still ACK, never spin the DLQ"
    assert seen == [str(Outcome.DEAD_LETTERED)]
