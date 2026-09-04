"""A staged COMPLETE must survive a lineage-publish failure — the outbox's whole purpose.

`publish_lineage_with_outbox` stages the event, publishes, then drops only on ack; on a publish
failure it re-raises **leaving the event staged** (`service_kit/lakehouse/outbox.py:147-170`). That
is the crash window working as designed.

The hazard: `stage_event` keys the staged file on `run_id` alone (`<outbox_uri>/<run_id>.json`,
truncating overwrite) and `build_run_event` seeds `run_id` from `(project, operation, token)` with
`event_type` **excluded**. So a COMPLETE and a FAIL for one run share a key. If the mover's error
path stages a FAIL after the COMPLETE publish failed, it destroys the COMPLETE — on a run whose Lance
write already committed, i.e. a run that SUCCEEDED.

`handle_stage` guards this with a flag. The flag must be set BEFORE the COMPLETE emit is attempted,
not after it returns — one line's difference, and the difference between a preserved event and a lost
one. These tests drive `handle_stage`, because that is where the guard lives; asserting on
`stage_event` alone would only re-state that overwrite is its documented behaviour.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest

import medallion.services.transform as mover
from medallion.core.config import MedallionSettings
from medallion.services import inprocess_executor
from medallion.services.compute import UpstreamFacts, WriteResult


def _settings(outbox_dir: Path) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "from_namespace": "bronze",
            "from_dataset": "bronze$events",
            "to_namespace": "silver",
            "to_dataset": "silver$features",
            "operation": "embed_features",
            "author": "data_eng",
            "sub_topic": "medallion.bronze",
            "lineage_outbox_uri": str(outbox_dir),
            "compute_enabled": True,
            "from_uri": "/tmp/from",
            "to_uri": "/tmp/to",
        }
    )


class _DaprFailingEveryPublish:
    """Every publish raises — so the COMPLETE emit fails and the error path runs."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []

    async def publish_event(self, *, data: str, **_: Any) -> None:
        self.attempts.append(json.loads(data))
        raise RuntimeError("sidecar down")


def _staged(outbox_dir: Path) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text()) for p in outbox_dir.glob("*.json")}


def _fake_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the read and the write: these tests exercise the EMIT path, not Lance."""
    monkeypatch.setattr(mover, "read_upstream", lambda uri, _so: UpstreamFacts(uri=uri, version=1))
    monkeypatch.setattr(inprocess_executor, "transform_stage", lambda *_a, **_k: WriteResult(version=1, row_count=1, size_bytes=1))


def test_a_failed_complete_publish_does_not_get_overwritten_by_a_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE REGRESSION GUARD.

    The Lance write commits, the COMPLETE publish fails leaving the COMPLETE staged, and the error
    path must NOT stage a FAIL over it. The run succeeded — redelivery re-publishes the staged
    COMPLETE, which is idempotent on its deterministic run_id.
    """
    _fake_stage(monkeypatch)
    dapr = _DaprFailingEveryPublish()
    settings = _settings(tmp_path)

    asyncio.run(mover.handle_stage(cast(Any, dapr), settings, {"data": {"token": "tok"}}))

    staged = _staged(tmp_path)
    assert staged, "nothing staged — the outbox did not run at all"
    assert len(staged) == 1, f"expected one staged event per run, got {list(staged)}"
    event_type = next(iter(staged.values())).get("eventType")
    assert event_type == "COMPLETE", (
        f"a {event_type} was staged over the COMPLETE — the outbox lost the event it exists to "
        "preserve. The guard flag in handle_stage is set after the COMPLETE emit instead of before it."
    )


def test_the_complete_is_still_staged_at_all_after_its_publish_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The outbox contract underneath the guard: a failed publish leaves the event staged, so the
    lineage relay can re-ingest it. If this regresses, the guard above protects nothing."""
    _fake_stage(monkeypatch)
    dapr = _DaprFailingEveryPublish()

    asyncio.run(mover.handle_stage(cast(Any, dapr), _settings(tmp_path), {"data": {"token": "tok"}}))

    assert _staged(tmp_path), "the crash window is gone — a publish failure now loses the event"
    assert dapr.attempts, "no publish was attempted"
    assert dapr.attempts[0].get("eventType") == "COMPLETE"
