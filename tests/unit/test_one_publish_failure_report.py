"""Every medallion publish reports its failure the same way (open_python-audit DUP-18).

Five call sites published a trigger and each wrapped it in its OWN `try/except` + `log.warning`, and
the five had drifted into four different reports:

* `ingest_trigger` / `train` — `{token, error}`, no topic, no traceback;
* `media_produce` — `{token, stage, error}`, no topic;
* `publication_trigger` — `{object_id, error}`, no TOKEN at all, so a failed publication trigger could
  not be joined to the cascade it belongs to;
* `promotion_hold` — `{token, dataset, topic}` plus `exc_info`, but no `error` string, so the one
  field the other four lead with is missing from the structured record.

Which fields a publish failure carries decides whether an operator can find the run it belongs to, and
that is not a per-call-site decision. `service_kit.dapr_publish.publish_json` is the one place it is
made — the same module the estate's single bounded `publish_event` already lives in.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest


REPO = Path(__file__).resolve().parents[2]
MEDALLION_SERVICES = REPO / "services/medallion/src/medallion/services"


class _Broken:
    """A Dapr client whose publish always fails — the condition all five sites report on."""

    async def publish_event(self, **_kwargs: Any) -> None:
        raise RuntimeError("nats unreachable")


def test_no_medallion_service_wraps_a_publish_in_its_own_try_except() -> None:
    """DUP-18: the try/except + warning around a publish belongs to the publish helper, not the caller.

    RED before the collapse: five modules under `services/medallion/services` each carried
    `await dapr_publish.publish_event(...)` inside their own `try:`.
    """
    offenders = sorted(
        str(path.relative_to(REPO)) for path in MEDALLION_SERVICES.rglob("*.py") if re.search(r"dapr_publish\.publish_event\(", path.read_text())
    )
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_a_failed_promotion_hold_records_the_error_string(caplog: pytest.LogCaptureFixture) -> None:
    """RED before the collapse: `promotion_hold` logged `exc_info` but no `error` field.

    A traceback reaches a human reading `kubectl logs`; the structured `error` is what the OTLP copy in
    GreptimeDB can be filtered on, and it is the field the other four sites lead with.
    """
    from medallion.core.config import MedallionSettings
    from medallion.services.promotion_hold import publish_hold
    from medallion.workflow import PromotionSpec

    settings = MedallionSettings()
    spec = PromotionSpec(
        token="tok-1",
        from_namespace="bronze",
        from_dataset="bronze.docs",
        to_namespace="silver",
        to_dataset="silver.docs",
        pub_topic=settings.pub_topic,
        reasons=["row_count_zero"],
        version=3,
    )
    with caplog.at_level(logging.WARNING):
        assert await publish_hold(_Broken(), settings, spec) is False
    record = next(r for r in caplog.records if r.message == "medallion_promotion_hold_not_published")
    assert getattr(record, "error", None) == "nats unreachable", record.__dict__
    assert getattr(record, "topic", None) == settings.promotion_topic, record.__dict__
    assert getattr(record, "token", None) == "tok-1", record.__dict__


@pytest.mark.asyncio
async def test_a_failed_publication_trigger_records_the_token(caplog: pytest.LogCaptureFixture) -> None:
    """RED before the collapse: `publication_trigger` reported `object_id` and no token.

    The token is what joins a failed trigger to the cascade it belongs to; `object_id` names the
    catalog object and cannot be used to find the run.
    """
    from medallion.core.config import MedallionSettings
    from medallion.services.publication_trigger import DELIMITER, PUBLISHED_ACTION, handle_publication

    settings = MedallionSettings.model_validate({"MEDALLION_TRANSFORM_ROUTES": {"bronze": "medallion.bronze"}})
    event = {
        "data": {
            "action": PUBLISHED_ACTION,
            "event_id": "evt-77",
            "object_id": f"table:bronze{DELIMITER}docs",
            "extra": {"from_version": 1, "to_version": 2},
        }
    }
    with caplog.at_level(logging.WARNING):
        result = await handle_publication(_Broken(), settings, event)
    assert result["status"] == "RETRY", result
    record = next(r for r in caplog.records if r.message == "medallion_publication_trigger_failed")
    assert getattr(record, "error", None) == "nats unreachable", record.__dict__
    assert getattr(record, "topic", None), record.__dict__
    assert getattr(record, "token", None), record.__dict__
