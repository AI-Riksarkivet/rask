"""#83 DLQ ops — the transactional-outbox view + on-demand replay endpoints.

Driven against a REAL local outbox (a tmp dir): stage events with ``common.outbox``, then exercise the
handlers directly. Covers the happy path (list summaries oldest-first, replay re-ingests + drops), the
poison object (surfaced in the view, un-replayable), the fail-closed auth ladder (FGA on + no token → 401),
per-dataset governance of the list, and the replay writer gate (a non-writer of the event's outputs is 403).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from common import fga, outbox
from common.audit import AUDIT_LOGGER, configure_audit
from common.oidc import IDToken
from lance_namespace import (
    PermissionDeniedError,
    TransactionNotFoundError,
    UnauthenticatedError,
    UnsupportedOperationError,
)
from lineage.api.fga_deps import DatasetFilter
from lineage.api.v1.endpoints import dlq
from lineage.core.config import LineageSettings, storage_options


_FULL_AUTH = {
    "oidc_enabled": True,
    "oidc_issuer": "https://dex.example",
    "oidc_audience": "lance",
    "fga_enabled": True,
    "fga_store_id": "store",
    "fga_model_id": "model",
}


def _settings(tmp: Path, **overrides: Any) -> LineageSettings:
    return LineageSettings.model_validate({"database_url": "postgresql://x/y", "outbox_uri": str(tmp), **overrides})


def _event_json(
    run_id: str = "r1",
    outputs: tuple[tuple[str, str], ...] = (("bronze", "bronze$x"),),
    inputs: tuple[tuple[str, str], ...] = (),
) -> str:
    return json.dumps(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-20T00:00:00Z",
            "run": {"runId": run_id, "facets": {}},
            "job": {"namespace": "ray-jobs", "name": "ingest"},
            "inputs": [{"namespace": ns, "name": n} for ns, n in inputs],
            "outputs": [{"namespace": ns, "name": n} for ns, n in outputs],
        }
    )


class _Repo:
    def __init__(self) -> None:
        self.ingested: list[Any] = []

    async def ingest_event(self, event: Any) -> None:
        self.ingested.append(event)

    async def record_event(self, **_kwargs: object) -> None:
        pass


def _request(**state: object) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state)))


def _token(sub: str = "alice") -> IDToken:
    return IDToken(iss="i", sub=sub, aud="lance", exp=0, iat=0)


def _filter(request: Any, settings: LineageSettings, token: Any) -> DatasetFilter:
    return DatasetFilter(request, settings, token)


def _stage(settings: LineageSettings, run_id: str, body: str) -> None:
    outbox.stage_event(settings.outbox_uri, storage_options(settings), run_id, body)


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
    s = LineageSettings.model_validate({"database_url": "postgresql://x/y"})  # outbox_uri = ""
    out = asyncio.run(
        dlq.list_dlq(s, None, _filter(_request(), s, None), limit=100)  # type: ignore[arg-type]
    )
    assert out.depth == 0 and out.events == []


def test_list_fails_closed_without_a_token_when_fga_on(tmp_path: Path) -> None:
    s = _settings(tmp_path, **_FULL_AUTH)
    with pytest.raises(UnauthenticatedError):
        asyncio.run(dlq.list_dlq(s, None, _filter(_request(fga=object()), s, None), limit=100))


def test_list_filters_events_to_visible_datasets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "r1", _event_json("r1", outputs=(("bronze", "mine"),)))
    _stage(s, "r2", _event_json("r2", outputs=(("bronze", "hidden"),)))

    async def only_mine(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        return {o: o.endswith(":mine") for o in objects}

    monkeypatch.setattr(fga, "batch_check", only_mine)
    token = _token()
    flt = _filter(_request(fga=object()), s, token)
    out = asyncio.run(dlq.list_dlq(s, token, flt, limit=100))
    assert out.depth == 2  # raw health signal unfiltered
    assert [e.run_id for e in out.events] == ["r1"]  # but only the visible dataset's event


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


def _replay(s: LineageSettings, run_id: str, repo: Any, token: Any = None) -> Any:
    flt = _filter(_request(fga=object()), s, token)
    return dlq.replay_dlq(run_id, _request(fga=object()), repo, s, token, flt)


def test_replay_reingests_and_drops(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _stage(s, "r1", _event_json("r1"))
    repo = _Repo()
    out = asyncio.run(_replay(s, "r1", repo))
    assert out.status == "replayed" and out.run_id == "r1"
    assert len(repo.ingested) == 1 and repo.ingested[0].run.run_id == "r1"
    # the staged object is gone (drop only runs on success)
    assert outbox.read_event(s.outbox_uri, storage_options(s), "r1") is None


def test_replay_missing_run_is_404(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    with pytest.raises(TransactionNotFoundError):
        asyncio.run(_replay(s, "nope", _Repo()))


def test_replay_poison_is_unsupported_and_not_dropped_dev(tmp_path: Path) -> None:
    # FGA off (dev): a poison object is honestly a 422, and is NOT destroyed by the failed replay.
    s = _settings(tmp_path)
    _stage(s, "bad", "{not valid json")
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(_replay(s, "bad", _Repo()))
    assert outbox.read_event(s.outbox_uri, storage_options(s), "bad") is not None


def test_replay_poison_is_404_under_fga_no_oracle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # FGA on: a poison object the governed list hides must NOT be distinguishable from missing (404, not 422).
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "bad", "{not valid json")

    async def any_visible(_c: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, True)

    monkeypatch.setattr(fga, "batch_check", any_visible)
    with pytest.raises(TransactionNotFoundError):
        asyncio.run(_replay(s, "bad", _Repo(), _token()))
    assert outbox.read_event(s.outbox_uri, storage_options(s), "bad") is not None


def test_replay_hidden_event_is_404_not_a_name_disclosing_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A caller who can't SEE the event's datasets gets 404 (indistinguishable from missing), NOT a 403 that
    # echoes 'bronze$secret' — the disclosure gap list_dlq closes, now closed on replay too.
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "r1", _event_json("r1", outputs=(("bronze", "bronze$secret"),)))

    async def deny_all(_c: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, False)

    monkeypatch.setattr(fga, "batch_check", deny_all)
    repo = _Repo()
    with pytest.raises(TransactionNotFoundError):
        asyncio.run(_replay(s, "r1", repo, _token()))
    assert repo.ingested == []
    assert outbox.read_event(s.outbox_uri, storage_options(s), "r1") is not None


def test_replay_seen_but_not_writable_is_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A caller who CAN see the dataset (can_get_metadata) but can't WRITE it → 403 (enforce_output_authz);
    # the name is one they can already see, so no new disclosure. Nothing ingested/dropped.
    s = _settings(tmp_path, **_FULL_AUTH)
    _stage(s, "r1", _event_json("r1", outputs=(("bronze", "bronze$x"),)))

    async def see_not_write(_c: object, *, relation: str, objects: list[str], **_kw: object) -> dict[str, bool]:
        return dict.fromkeys(objects, relation == "can_get_metadata")

    monkeypatch.setattr(fga, "batch_check", see_not_write)
    repo = _Repo()
    with pytest.raises(PermissionDeniedError):
        asyncio.run(_replay(s, "r1", repo, _token()))
    assert repo.ingested == []
    assert outbox.read_event(s.outbox_uri, storage_options(s), "r1") is not None


def list_dlq_off(settings: LineageSettings) -> Any:
    """list_dlq with FGA off (governed is pass-through) — the common view driver."""
    return dlq.list_dlq(settings, None, _filter(_request(), settings, None), limit=100)


# --------------------------------------------------------------------------- #
# Audit (#41): every replay lands on the lance.audit stream, house vocabulary
# --------------------------------------------------------------------------- #


class _CaptureAudit(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def audit_records() -> Iterator[list[logging.LogRecord]]:
    """Capture the dedicated ``lance.audit`` stream with the trail enabled (as the lifespan configures)."""
    handler = _CaptureAudit()
    logger = logging.getLogger(AUDIT_LOGGER)
    configure_audit(enabled=True)
    logger.addHandler(handler)
    try:
        yield handler.records
    finally:
        logger.removeHandler(handler)
        configure_audit(enabled=True)  # leave the stream on for the rest of the suite


def test_replay_audits_success_with_the_actor(tmp_path: Path, audit_records: list[logging.LogRecord]) -> None:
    # House vocabulary: a COMPLETED action records `success` (ALLOW/DENY belong to authz decisions) — an
    # authenticated governed replay must never surface as outcome=allow (the inverted pre-fix record).
    s = _settings(tmp_path)
    _stage(s, "r1", _event_json("r1"))
    asyncio.run(_replay(s, "r1", _Repo(), _token()))
    assert len(audit_records) == 1
    fields = {k: v for k, v in audit_records[0].__dict__.items() if k.startswith("audit.")}
    assert fields == {
        "audit.action": "dlq_replay",
        "audit.outcome": "success",
        "audit.subject": "alice",
        "audit.resource": "run:r1",
    }


def test_replay_audits_success_without_a_subject_in_dev(tmp_path: Path, audit_records: list[logging.LogRecord]) -> None:
    # Auth-off dev: still a success record, with an empty subject (no principal to attribute).
    s = _settings(tmp_path)
    _stage(s, "r1", _event_json("r1"))
    asyncio.run(_replay(s, "r1", _Repo()))
    fields = audit_records[0].__dict__
    assert fields["audit.outcome"] == "success" and fields["audit.subject"] == ""


def test_lineage_audit_stream_is_env_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    # The lifespan gates `lance.audit` on the SHARED LANCE_AUDIT_ENABLED (catalog parity — one flag for
    # the estate's compliance posture): default on, and the env alias turns the stream off.
    assert LineageSettings.model_validate({"database_url": "postgresql://x/y"}).audit_enabled is True
    monkeypatch.setenv("LANCE_AUDIT_ENABLED", "false")
    assert LineageSettings(database_url="postgresql://x/y").audit_enabled is False
