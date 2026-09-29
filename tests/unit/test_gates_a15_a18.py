"""A15–A18 as named gates, against the real stage runner and the real chart values.

The four claims the plan makes about the cascade ABOVE bronze. Each was prose; each is now a test
that fails if the behaviour changes.

Everything here drives `medallion.services.transform.handle_stage` — the same function the deployed
stage runner's Dapr subscription calls — against real Lance in a temp directory. No mock stands in for the
thing under test: a gate over a double asserts that the double behaves, which is the failure mode
the estate has been burned by (a Tiltfile that could never have worked, an ingest queue nothing
drained; both looked healthy from every angle except an actual run).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import lance
import pytest
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services import transform
from medallion.services.catalog_register import PublishOutcome
from medallion.services.produce import produce
from medallion.services.transform import handle_stage


class _FakeDapr:
    """Captures published events. A publish is the OBSERVABLE side effect these gates assert on —
    whether downstream was woken, and with what."""

    def __init__(self, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self.fail = fail

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        if self.fail:
            raise RuntimeError("pubsub unavailable")
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def _bronze(tmp_path: Path) -> str:
    uri = str(tmp_path / "bronze")
    settings = MedallionSettings.model_validate({"compute_enabled": True, "bronze_uri": uri})
    asyncio.run(produce(cast(DaprClient, _FakeDapr()), settings, token="idem-test"))
    return uri


def _stage_runner(tmp_path: Path, **overrides: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "compute_enabled": True,
        "from_uri": str(tmp_path / "bronze"),
        "to_uri": str(tmp_path / "silver"),
        "from_namespace": "bronze",
        "from_dataset": "bronze$events",
        "to_namespace": "silver",
        "to_dataset": "silver$features",
        "operation": "embed",
        "pub_topic": "medallion.silver",
    }
    return MedallionSettings.model_validate({**base, **overrides})


# ── A17 — the stage runner contract (E1–E3) ──────────────────────────────────────────────────


def test_a17_a_redelivered_event_is_a_NO_OP_not_a_second_transform(tmp_path: Path) -> None:
    """E2. Redelivery is normal on an at-least-once bus, not exceptional.

    The same trigger token delivered twice must converge on one silver, not append a second copy of
    every row. Asserted on ROW COUNT rather than on a call count: a stage runner that "ran twice but wrote
    the same rows" is correct, and one that ran once but doubled the rows is not — only the data can
    tell them apart.
    """
    _bronze(tmp_path)
    settings = _stage_runner(tmp_path)
    dapr = _FakeDapr()

    first = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-1"}}))
    rows_after_first = lance.dataset(str(tmp_path / "silver")).count_rows()

    second = asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-1"}}))
    rows_after_second = lance.dataset(str(tmp_path / "silver")).count_rows()

    assert first == {"status": "SUCCESS"}
    assert second == {"status": "SUCCESS"}
    assert rows_after_second == rows_after_first, "a redelivered trigger duplicated rows — the hop is not idempotent"


def test_a17_a_publish_outage_returns_RETRY_rather_than_swallowing_the_hop(tmp_path: Path) -> None:
    """The RETRY contract — the entry point to resiliency, maxDeliver and the DLQ.

    A stage runner that cannot announce its output must NOT report success: downstream would never be woken
    and the data would sit in silver looking finished. Returning RETRY is what puts the event back on
    the bus and, after maxDeliver, into the DLQ where an operator can see it.
    """
    _bronze(tmp_path)
    settings = _stage_runner(tmp_path)

    result = asyncio.run(handle_stage(cast(DaprClient, _FakeDapr(fail=True)), settings, {"data": {"token": "tok"}}))

    assert result == {"status": "RETRY"}


# ── A18 — publication behaviour ───────────────────────────────────────────────────────


def test_a18_a_HELD_batch_still_leaves_a_lineage_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hold must be VISIBLE. Silence is indistinguishable from a run that never started.

    The medallion's original defect in miniature: it emitted only on COMPLETE, so a failure left no
    record at all. A held batch is a decision the estate made about data — it belongs in the graph.

    THE HOLD NOW COMES FROM THE CATALOG, and this test had to move with it. It used to hand the stage runner
    a required column that does not exist and let the stage runner's own `assert_quality` refuse. Under one
    door the stage runner measures and does not rule (its assertions still populate the
    `dataQualityAssertions` facet, which is why the audit half is unchanged), so a refusal has exactly
    one source: `publish` answering `published=False`.

    The property under test is A18 itself — that a refusal reaches the GRAPH — not which component
    refused. Wiring the refusal to its real source is what keeps that property tested rather than
    quietly untested, which is what deleting the assertion would have done.
    """
    _bronze(tmp_path)
    settings = _stage_runner(tmp_path, catalog_url="http://catalog.invalid")
    monkeypatch.setattr(
        transform.catalog_register,
        "publish_stage_output",
        lambda **_: PublishOutcome(published=False, failed_assertions=["a_column_that_does_not_exist"]),
    )
    dapr = _FakeDapr()

    asyncio.run(handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok"}}))

    lineage = [p for p in dapr.published if p["topic"] == settings.lineage_topic]
    assert lineage, "a held batch left no lineage record at all"
    assert any(p["data"]["eventType"] == "FAIL" for p in lineage), "a held batch was not recorded as a FAIL"
