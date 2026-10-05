"""Activity-body defects that only show up when an activity RUNS TWICE, or when it fails.

Dapr guarantees at-least-once activity execution: a worker that crashes after doing the work but
before recording the result re-executes the whole body on recovery. Everything an activity does must
therefore be safe to do twice, and everything it swallows must be findable afterwards.

1. DWF-ACT-002 -- `request_approval` minted a fresh `event_id` per execution, so a re-executed
   activity double-notified the approver. `event_id` is documented as "the client-side dedupe key",
   and a `uuid4()` default makes it a fresh key every time, which is the one value that cannot dedupe.
2. `emit_promotion_outcome`'s failure log named NOTHING -- no token, no dataset, no decider -- while
   this activity's own docstring calls lineage "the durable record". A dropped publish emptied the
   record and left a log line nobody could tie to a promotion.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from medallion.schemas.promotion import PromotionSpec
from medallion.workflow import request_approval


def _spec(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "acme-silver",
        "from_dataset": "acme-silver$features",
        "to_namespace": "acme-gold",
        "to_dataset": "acme-gold$catalog",
        "operation": "aggregate_gold",
        "author": "analyst",
        "version": 7,
        "reasons": ["row_delta_band"],
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "approval_hours": 72,
    }
    return PromotionSpec.model_validate(base | over).model_dump()


def _published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the control events `request_approval` publishes, without a sidecar.

    Patched at `medallion.workflow`'s own import site: the activity imports `publish_event` LOCALLY
    inside the function body, so patching the defining module binds nothing the call will look at --
    the same local-import trap `test_fanin_return_ceiling` records for ingest.
    """
    seen: list[dict[str, Any]] = []

    async def _publish(_client: Any, *, timeout_seconds: float, data: str = "", **_kw: Any) -> None:
        seen.append(json.loads(data))

    monkeypatch.setattr("service_kit.dapr_publish.publish_event", _publish)

    class _Dapr:
        async def __aenter__(self) -> _Dapr:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("dapr.aio.clients.DaprClient", lambda *a, **k: _Dapr())
    return seen


def test_a_RE_EXECUTED_request_approval_carries_the_SAME_dedupe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WEDGE. Dapr re-executes an activity whose result was not recorded; a fresh uuid4 per
    execution is precisely the key that cannot dedupe, so the approver is asked twice."""
    seen = _published(monkeypatch)

    request_approval(cast("Any", None), PromotionSpec.model_validate(_spec()))
    request_approval(cast("Any", None), PromotionSpec.model_validate(_spec()))

    assert len(seen) == 2, f"the fixture did not capture both publishes: {seen}"
    assert seen[0]["event_id"] == seen[1]["event_id"], f"a re-executed activity minted a fresh dedupe key: {seen[0]['event_id']} vs {seen[1]['event_id']}"
