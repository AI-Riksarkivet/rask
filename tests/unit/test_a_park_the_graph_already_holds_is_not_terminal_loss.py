"""`DEAD_LETTERED` must mean the graph is MISSING the event, which is what its two comments promise.

`metrics.Outcome.DEAD_LETTERED` is labelled "terminal loss signal" and `on_dead_letter` states that "a
dashboardable non-zero here means the graph is missing events". Measured against the live estate
2026-09-13, that was false for most of the population: the DLQ held 8,515 parked deliveries of
`lineage.events.v1` while the stream's last sequence was 5,860 — so events were parked repeatedly — and
of the 21 distinct parked run ids sampled, 17 were already present in the AGE graph. The ids are
deterministic (UUIDv5) and `ingest_event` MERGEs on `run_id`, so a stage that runs again re-emits the
same id and the node lands; every later park of that run is visibility, not loss.

Counting those as loss is what inflated the signal by roughly two orders of magnitude. The question
"is this run in the graph?" is a point read the repository already exposes (`run_status`), so the
parking route can answer it instead of assuming the worst.

The fallbacks all lean the SAME way — toward reporting loss. No run id, no repository, or a repository
that raises all record `DEAD_LETTERED`: a parking route that cannot ask must not answer "nothing was
lost", and an unreachable graph is the moment the signal matters most.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.ns_errors import install_problem_handlers


class _Repo:
    """Stands in for `LineageRepository` on `app.state` — only `run_status` is consulted here."""

    def __init__(self, *, known: set[str] | None = None, raises: bool = False) -> None:
        self.known = known or set()
        self.raises = raises
        self.asked: list[str] = []

    async def run_status(self, run_id: str) -> object | None:
        self.asked.append(run_id)
        if self.raises:
            raise RuntimeError("graph unreachable")
        return object() if run_id in self.known else None


def _parking_client(monkeypatch: pytest.MonkeyPatch, repo: _Repo | None) -> TestClient:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    monkeypatch.setenv("LINEAGE_DAPR_ENABLED", "true")
    monkeypatch.setenv("LINEAGE_DLQ_TOPIC", "dlq.lineage.events")

    import lineage.api.dapr as dapr_mod
    from lineage.core.config import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    install_problem_handlers(app, logging.getLogger(__name__))
    dapr_mod.register_dapr(app)
    if repo is not None:
        app.state.repository = repo
    get_settings.cache_clear()
    return TestClient(app)


def _park(client: TestClient, payload: dict[str, Any]) -> dict[str, str]:
    response = client.post("/lineage-dlq", json=payload, headers={"dapr-api-token": "s3cret"})
    assert response.status_code == 200
    return response.json()


def _event(run_id: str | None) -> dict[str, Any]:
    run: dict[str, Any] = {"facets": {"author": {"sub": "alice"}}}
    if run_id is not None:
        run["runId"] = run_id
    return {"id": "evt-1", "data": {"run": run, "eventType": "COMPLETE"}}


def _outcomes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import lineage.api.dapr as dapr_mod

    seen: list[str] = []
    monkeypatch.setattr(dapr_mod, "record_outcome", lambda outcome: seen.append(str(outcome)))
    return seen


def test_a_park_whose_run_the_graph_already_holds_is_not_counted_as_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known={"run-abc"})
    client = _parking_client(monkeypatch, repo)

    assert _park(client, _event("run-abc")) == {"status": "SUCCESS"}  # still ACKs — never a retry loop
    assert repo.asked == ["run-abc"], "the parking route must ask the graph before calling it loss"
    assert seen == [str(Outcome.PARKED_ALREADY_RECORDED)], f"a re-park of a recorded run is not terminal loss, got {seen}"


def test_a_park_whose_run_the_graph_lacks_is_still_terminal_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    client = _parking_client(monkeypatch, _Repo(known=set()))

    assert _park(client, _event("run-missing")) == {"status": "SUCCESS"}
    assert seen == [str(Outcome.DEAD_LETTERED)], f"an absent run is the loss the counter exists for, got {seen}"


def test_a_recorded_park_logs_at_warning_and_a_lost_one_at_error(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The two answers send an operator to different places, so they must not share a severity: an
    ERROR per re-park is what let the real losses scroll away among them."""
    client = _parking_client(monkeypatch, _Repo(known={"run-abc"}))
    with caplog.at_level(logging.WARNING, logger="lineage.api.dapr"):
        _park(client, _event("run-abc"))
    recorded = next(r for r in caplog.records if r.message == "dapr_dead_letter_parked")
    assert recorded.levelno == logging.WARNING
    assert getattr(recorded, "already_recorded", None) is True
    assert getattr(recorded, "author", None) == "alice", "the author must survive the new branch"

    caplog.clear()
    client_missing = _parking_client(monkeypatch, _Repo(known=set()))
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
    client = _parking_client(monkeypatch, repo)
    assert _park(client, _event("run-abc")) == {"status": "SUCCESS"}
    assert seen == [str(Outcome.DEAD_LETTERED)], f"an unanswerable question is not an answer of 'no loss', got {seen}"


def test_a_park_carrying_no_readable_run_id_reports_loss(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.core.metrics import Outcome

    seen = _outcomes(monkeypatch)
    repo = _Repo(known={"run-abc"})
    client = _parking_client(monkeypatch, repo)
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
