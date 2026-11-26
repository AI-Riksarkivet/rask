"""#83 DLQ ops — the transactional-outbox view + on-demand replay endpoints.

Driven against a REAL local outbox (a tmp dir): stage events with ``lakehouse.events.outbox``, then exercise the
handlers directly. Covers the happy path (list summaries oldest-first, replay re-ingests + drops), the
poison object (surfaced in the view, un-replayable), the fail-closed auth ladder (FGA on + no token → 401),
per-dataset governance of the list, and the replay writer gate (a non-writer of the event's outputs is 403). Who may see
or replay what is judged by the real model on an OpenFGA store of the test's own, where bob owns the staged events'
tables so each is governed.
"""

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from lance_namespace import (
    InvalidInputError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TransactionNotFoundError,
    UnauthenticatedError,
    UnsupportedOperationError,
)
from lineage_lane import decided

from lakehouse.events import outbox
from lakehouse.events.cloud_events import lineage_envelope
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.governed.decider import OpenFgaDecider
from lineage.api.dependencies import request_settings
from lineage.api.fga_deps import DatasetFilter
from lineage.api.security import authenticate
from lineage.api.v1.endpoints import dlq
from lineage.core.config import LineageSettings, storage_options
from lineage.models import RunEvent
from lineage.services.provenance import ProvenanceStore
from tests.app_state import app_holding, request_on
from tests.callers import person
from tests.provenance_store_double import ProvenanceStoreDouble


_FULL_AUTH = {
    "oidc_enabled": True,
    "oidc_issuer": "https://dex.example",
    "oidc_audience": "lance",
    "oidc_idp_id": "dex",
    "oidc_connectors": {"local": "dex"},
    "fga_enabled": True,
    "fga_store_id": "store",
}


def _settings(tmp: Path, **overrides: Any) -> LineageSettings:
    return LineageSettings.model_validate({"outbox_uri": str(tmp), **overrides})


def _uuid(name: str) -> str:
    """The uuid the catalog minted for the table ``name``, which its staged events carry (ADR 0173 §10)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


def _dataset(namespace: str, name: str) -> dict[str, Any]:
    """A governed table as the catalog names it on an event: its name, and its uuid in the ``rask_lance`` facet."""
    return {"namespace": namespace, "name": name, "facets": {"rask_lance": {"tableUuid": _uuid(name)}}}


def _event_json(
    run_id: str = "r1",
    outputs: tuple[tuple[str, str], ...] = (("bronze", "bronze$x"),),
    inputs: tuple[tuple[str, str], ...] = (),
    author: str | None = None,
) -> str:
    return json.dumps(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-20T00:00:00Z",
            "run": {"runId": run_id, "facets": {"rask_author": {"name": author, "sub": author}} if author else {}},
            "job": {"namespace": "pipelines", "name": "ingest"},
            "inputs": [_dataset(ns, n) for ns, n in inputs],
            "outputs": [_dataset(ns, n) for ns, n in outputs],
        }
    )


def _request(**state: object) -> Request:
    return request_on(**state)


def _held(store: Any, *rungs: tuple[str, str, str]) -> Any:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """``store`` with bob owning every table a staged event below names, and ``rungs`` as ``(person, relation, table)``."""
    names = ("mine", "hidden", "bronze$secret", "bronze$x")
    store.write(
        *((fga_ids.user_object("dex~bob"), "owner", fga_ids.table_object(_uuid(name))) for name in names),
        *((fga_ids.user_object(f"dex~{who}"), relation, fga_ids.table_object(_uuid(name))) for who, relation, name in rungs),
    )
    return store


def _filter(request: Any, settings: LineageSettings, token: Any) -> DatasetFilter:
    return DatasetFilter(request, settings, token)


def _stage(settings: LineageSettings, run_id: str, body: str) -> None:
    """Stage ``body`` under ``run_id``: an event in the envelope a producer stages it in, and bytes that are no event as
    they are, which is what a poison object is."""
    try:
        staged = json.dumps(lineage_envelope(json.loads(body)).as_wire())
    except ValueError:
        staged = body
    outbox.stage_event(settings.outbox_uri, storage_options(settings), run_id, event_json=staged)


# --------------------------------------------------------------------------- #
# The view
# --------------------------------------------------------------------------- #


def test_list_returns_backlog_and_summaries_oldest_first(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _stage(s, "r1", _event_json("r1", outputs=(("bronze", "bronze$a"),)))
    _stage(s, "r2", _event_json("r2", outputs=(("bronze", "bronze$b"),)))
    out = asyncio.run(list_dlq_off(s))
    assert out.depth == 2
    assert [e.run_id for e in out.events] == ["r1", "r2"]  # oldest first
    assert out.events[0].outputs == ["bronze$a"]
    assert out.events[0].event_type == "COMPLETE" and out.events[0].parseable


def test_list_surfaces_a_poison_object(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _stage(s, "bad", "{not valid json")
    out = asyncio.run(list_dlq_off(s))
    assert out.depth == 1
    assert out.events[0].run_id == "bad" and out.events[0].parseable is False


def test_list_empty_when_no_outbox_configured() -> None:
    s = LineageSettings.model_validate({})  # outbox_uri = ""
    out = asyncio.run(dlq.list_dlq(settings=s, datasets=_filter(_request(), s, None), limit=100))
    assert out.depth == 0 and out.events == []


@pytest.mark.parametrize(
    ("decider", "caller", "refusal", "saying"),
    [
        pytest.param(OpenFgaDecider(cast("Any", object())), None, UnauthenticatedError, "authentication required", id="no-caller"),
        pytest.param(None, person(), ServiceUnavailableError, "authorization service is not available", id="no-decider"),
    ],
)
def test_list_fails_closed_when_fga_is_on_and_the_gate_cannot_ask(decider: OpenFgaDecider | None, caller: Any, refusal: type[Exception], saying: str) -> None:
    # No outbox, so the door has nothing to scope and only its own gate stands between the request and the depth: an
    # anonymous caller, and a caller nothing can be asked about while the lifespan holds no decider.
    s = LineageSettings.model_validate(_FULL_AUTH)
    app = app_holding(decider=decider)
    app.include_router(dlq.router)
    app.dependency_overrides[request_settings] = lambda: s
    app.dependency_overrides[authenticate] = lambda: caller
    with pytest.raises(refusal, match=saying):
        TestClient(app).get("/admin/dlq")


def test_list_filters_events_to_visible_datasets(tmp_path: Path, openfga_store: Any) -> None:  # noqa: ANN401
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "r1", _event_json("r1", outputs=(("bronze", "mine"),)))
    _stage(s, "r2", _event_json("r2", outputs=(("bronze", "hidden"),)))
    token = person()

    async def listed(decider: OpenFgaDecider) -> Any:  # noqa: ANN401 — the door's answer
        return await dlq.list_dlq(settings=s, datasets=_filter(_request(decider=decider), s, token), limit=100)

    out = decided(_held(openfga_store, ("alice", "reader", "mine")), listed)
    assert out.depth == 2  # raw health signal unfiltered
    assert [e.run_id for e in out.events] == ["r1"]  # but only the visible dataset's event


def test_a_caller_pages_past_events_it_may_not_see_to_the_one_it_may(tmp_path: Path, openfga_store: Any) -> None:  # noqa: ANN401
    # Two events alice may not see are staged ahead of hers: a page covers two staged events whoever may see them, so
    # the first shows her none and answers the cursor that reaches hers.
    s = _settings(tmp_path, **_FULL_AUTH)
    for second, (run_id, table) in enumerate((("r1", "hidden"), ("r2", "bronze$secret"), ("r3", "mine")), start=1):
        _stage(s, run_id, _event_json(run_id, outputs=(("bronze", table),)))
        # The outbox is read in the order its objects were last modified, so each is given a time of its own.
        os.utime(tmp_path / f"{run_id}.json", ns=(second * 10**9, second * 10**9))
    token = person()

    async def paged(decider: OpenFgaDecider) -> list[tuple[list[str], bool]]:
        pages: list[tuple[list[str], bool]] = []
        after: str | None = None
        while True:
            page = await dlq.list_dlq(settings=s, datasets=_filter(_request(decider=decider), s, token), after=after, limit=2)
            pages.append(([e.run_id for e in page.events], page.next_cursor is not None))
            after = page.next_cursor
            if after is None or len(pages) == 3:
                return pages

    assert decided(_held(openfga_store, ("alice", "reader", "mine")), paged) == [([], True), (["r3"], False)]


def test_a_cursor_the_door_did_not_issue_is_refused(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(InvalidInputError, match="`after` is not a cursor this door issued"):
        asyncio.run(dlq.list_dlq(settings=s, datasets=_filter(_request(), s, None), after="bm90IGEgY3Vyc29y", limit=2))


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def _replay(s: LineageSettings, run_id: str, repo: ProvenanceStoreDouble, token: Any = None, decider: Any = None) -> Any:  # noqa: ANN401 — a decider, or none with FGA off
    decider = decider if decider is not None else OpenFgaDecider(cast("Any", object()))
    flt = _filter(_request(decider=decider), s, token)
    store = cast(ProvenanceStore, repo)
    # The door's feed is the one lineage's lifespan wires on its app, which the run-author check reads.
    return dlq.replay_dlq(
        run_id=run_id, request=_request(decider=decider, provenance=store), provenance=store, settings=s, token=token, datasets=flt, publisher=None
    )


def test_replay_reingests_and_drops(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _stage(s, "r1", _event_json("r1"))
    repo = ProvenanceStoreDouble()
    out = asyncio.run(_replay(s, "r1", repo))
    assert out.status == "replayed" and out.run_id == "r1"
    assert len(repo.recorded) == 1 and isinstance(repo.recorded[0], RunEvent) and repo.recorded[0].run.run_id == "r1"
    # the staged object is gone (drop only runs on success)
    assert outbox.resolve_event(s.outbox_uri, storage_options(s), "r1") is None


def test_replay_missing_run_is_404(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(TransactionNotFoundError):
        asyncio.run(_replay(s, "nope", ProvenanceStoreDouble()))


def test_replay_poison_is_unsupported_and_not_dropped_dev(tmp_path: Path) -> None:
    # FGA off (dev): a poison object is honestly a 422, and is NOT destroyed by the failed replay.
    s = _settings(tmp_path)
    _stage(s, "bad", "{not valid json")
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(_replay(s, "bad", ProvenanceStoreDouble()))
    assert outbox.resolve_event(s.outbox_uri, storage_options(s), "bad") is not None


def test_replay_poison_is_404_under_fga_no_oracle(tmp_path: Path, openfga_store: Any) -> None:  # noqa: ANN401
    # FGA on: a poison object the governed list hides must NOT be distinguishable from missing (404, not 422), to a
    # caller who reads every table the outbox's events name.
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "bad", "{not valid json")
    store = _held(openfga_store, *(("alice", "reader", name) for name in ("mine", "hidden", "bronze$secret", "bronze$x")))
    with pytest.raises(TransactionNotFoundError):
        decided(store, lambda decider: _replay(s, "bad", ProvenanceStoreDouble(), person(), decider))
    assert outbox.resolve_event(s.outbox_uri, storage_options(s), "bad") is not None


@pytest.mark.parametrize(
    "outputs",
    [
        pytest.param((("bronze", "bronze$secret"),), id="names-a-table-the-caller-cannot-see"),
        pytest.param((), id="names-no-table"),
    ],
)
def test_replay_of_an_event_the_list_hides_is_404_and_leaves_it_staged(
    tmp_path: Path,
    openfga_store: Any,  # noqa: ANN401 — the root conftest's OpenFgaStore
    outputs: tuple[tuple[str, str], ...],
) -> None:
    # The list hides both from a caller holding no grant, so a replay answers each as it answers a missing run: a 403
    # would name `bronze$secret`, and a replay of the event that names no table would record and drop another author's.
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "r1", _event_json("r1", outputs=outputs, author="dex~bob"))
    repo = ProvenanceStoreDouble()
    with pytest.raises(TransactionNotFoundError):
        decided(_held(openfga_store), lambda decider: _replay(s, "r1", repo, person(), decider))
    assert repo.recorded == []
    assert outbox.resolve_event(s.outbox_uri, storage_options(s), "r1") is not None


def test_replay_seen_but_not_writable_is_403(tmp_path: Path, openfga_store: Any) -> None:  # noqa: ANN401
    # A caller who CAN see the dataset (can_get_metadata) but can't WRITE it → 403 (enforce_output_authz);
    # the name is one they can already see, so no new disclosure. Nothing ingested/dropped.
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "r1", _event_json("r1", outputs=(("bronze", "bronze$x"),), author="dex~alice"))
    repo = ProvenanceStoreDouble()
    with pytest.raises(PermissionDeniedError, match=r"can_write_data required on outputs: bronze\$x"):
        decided(_held(openfga_store, ("alice", "reader", "bronze$x")), lambda decider: _replay(s, "r1", repo, person(), decider))
    assert repo.recorded == []
    assert outbox.resolve_event(s.outbox_uri, storage_options(s), "r1") is not None


def list_dlq_off(settings: LineageSettings) -> Any:
    """list_dlq with FGA off (governed is pass-through) — the common view driver."""
    return dlq.list_dlq(settings=settings, datasets=_filter(_request(), settings, None), limit=100)


# --------------------------------------------------------------------------- #
# Audit (#41): every replay lands on the lance.audit stream, house vocabulary
# --------------------------------------------------------------------------- #


def test_replay_audits_success_with_the_actor(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # House vocabulary: a COMPLETED action records `success` (ALLOW/DENY belong to authz decisions) — an
    # authenticated governed replay must never surface as outcome=allow (the inverted pre-fix record).
    s = _settings(tmp_path)
    _stage(s, "r1", _event_json("r1"))
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        asyncio.run(_replay(s, "r1", ProvenanceStoreDouble(), person()))
    audit_records = [record for record in caplog.records if record.name == AUDIT_LOGGER]
    assert len(audit_records) == 1
    fields = {k: v for k, v in audit_records[0].__dict__.items() if k.startswith("audit.")}
    assert fields == {
        "audit.format": "2.0",
        "audit.action": "dlq_replay",
        "audit.outcome": "success",
        "audit.trace_id": "-",
        "audit.subject": "dex~alice",
        "audit.resource": "run:r1",
    }
