"""The relay tick answers only its sidecar's cron binding, at the binding's exact name (ADR 0158).

The tick drains the lineage outbox, prunes the feed and forwards it to the graph backend, so a caller who could drive
it could re-publish whatever is staged and push the feed out on demand. The route is mounted only when the chart names
a binding, answers Dapr's discovery pre-flight without the app token (the probe carries none), and refuses a POST
without it. Driven through lineage's router over the real store in a live catalog (`lineage_app`), with a recording
graph backend, so a refused tick is seen to forward nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from service_kit.lakehouse.ns_errors import install_problem_handlers
from tests.integration.lineage_app import RELAY, SUBJECT_HEADER, governed_lineage, graph_backend, live_catalog, run_event


_TABLE = "acme-silver$pages"


def test_the_app_mounts_the_tick_only_when_a_binding_names_it() -> None:
    from lineage.main import mount_relay_cron

    bare, mounted = FastAPI(), FastAPI()
    install_problem_handlers(bare, logging.getLogger(__name__))
    install_problem_handlers(mounted, logging.getLogger(__name__))

    assert (mount_relay_cron(bare, None), mount_relay_cron(bare, ""), mount_relay_cron(mounted, RELAY)) == (False, False, True)
    assert (TestClient(bare).options(f"/{RELAY}").status_code, TestClient(mounted).options(f"/{RELAY}").status_code) == (404, 200)


def test_the_discovery_preflight_is_answered_at_the_binding_name_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url, governed_lineage(catalog_url, monkeypatch, set()) as lineage:
        answers = (lineage.options(f"/{RELAY}"), lineage.options("/other-binding"))

    assert (answers[0].json(), answers[1].status_code) == ({"status": "ok"}, 404)


def test_a_tick_without_the_sidecars_token_forwards_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every count is in the report when it is zero: a count that appears only when it did something makes "nothing
    happened" and "the step never ran" the same observation."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    run = run_event("1d3c5a07-0000-4000-8000-000000000593", "COMPLETE", "2026-10-15T10:00:00+00:00", outputs=[_TABLE])
    with graph_backend() as (graph, url):
        monkeypatch.setenv("LINEAGE_FORWARD_URL", url)
        with (
            live_catalog(tmp_path / "catalog", monkeypatch) as catalog_url,
            governed_lineage(catalog_url, monkeypatch, {("alice", "can_write_data", f"table:{_TABLE}")}) as lineage,
        ):
            assert lineage.post("/api/v1/lineage", json=run, headers={SUBJECT_HEADER: "alice"}).status_code == 201
            refused = [lineage.post(f"/{RELAY}").status_code, lineage.post(f"/{RELAY}", headers={"dapr-api-token": "wrong"}).status_code]
            sent_while_refused = len(graph.received)
            tick = lineage.post(f"/{RELAY}", headers={"dapr-api-token": "s3cret"})

    assert (refused, sent_while_refused) == ([403, 403], 0)
    assert tick.json() == {
        "outbox_drained": 0,
        "outbox_stranded": 0,
        "outbox_refused": 0,
        "outbox_refusals_recorded": 0,
        "pruned_events": 0,
        "forwarded": 1,
        "forward_refused": 0,
        "forward_stalled": None,
    }
