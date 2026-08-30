"""The media DROP path's FAIL emit is the compensating control, and it ran in no test.

`transform.py`'s `UnderivableMediaError` branch returns `_DROP`, so Dapr will **not** redeliver — the
code's own comment states what that costs if the emit is lost: "a lost FAIL publish means the failed
run is NEVER recorded and NEVER retried: the graph silently forgets it." The FAIL event is therefore the
only record that the run happened at all, and nothing executed it.

Compounding it, the emit sat inside `with suppress(Exception)`, so a defect INSIDE the compensating
control produced silence rather than a red — the precise failure the control exists to prevent. The
suppression itself is correct and stays (a graph outage must not convert a correct refusal into a retry
storm); what it must not do is discard the diagnosis, so the four sites now run under `_best_effort`,
which logs.

Targeting matters as much as existence here. Per `.claude/skills/rask-notifications`, the mover authors
with a chart ROLE LITERAL, so `author_subject()` addresses an inbox actor named `data_eng` — nobody. A
failed media run reaches the person who asked for it only through `lance.originator`, which is why this
file asserts the FAIL event carries it rather than merely asserting a FAIL was emitted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient

from medallion.core.config import MedallionSettings
from medallion.services import transform as tf
from medallion.services.derivers import UnderivableMediaError


class _FakeDapr:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, *, pubsub_name: str, topic_name: str, data: str, data_content_type: str) -> None:
        self.published.append({"topic": topic_name, "data": json.loads(data)})


def _settings(tmp_path: Any) -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "compute_enabled": True,
            "from_uri": str(tmp_path / "bronze"),
            "to_uri": str(tmp_path / "silver"),
            "from_namespace": "bronze",
            "from_dataset": "bronze$media",
            "to_namespace": "silver",
            "to_dataset": "silver$derived",
            "operation": "derive",
            "pub_topic": "medallion.silver",
        }
    )


@pytest.fixture
def underivable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the stage's own work raise the deterministic bad-media error."""

    def _boom(*_a: object, **_k: object) -> None:
        raise UnderivableMediaError("payload matched the content probe but cannot decode")

    # Raised from the READ rather than the transform: the branch under test is keyed on the exception
    # type, not on where in the stage it came from, and the read is the first thing inside the `try`
    # — so this reaches the handler without needing a real upstream dataset on disk.
    monkeypatch.setattr(tf, "read_upstream", _boom)


def _fail_events(dapr: _FakeDapr, settings: MedallionSettings) -> list[dict[str, Any]]:
    return [p["data"] for p in dapr.published if p["topic"] == settings.lineage_topic and p["data"].get("eventType") == "FAIL"]


def test_underivable_media_records_a_FAIL_run_and_drops(tmp_path: Any, underivable: None) -> None:
    """DROP is right — redelivery cannot fix bytes — but only if the run is recorded first."""
    dapr, settings = _FakeDapr(), _settings(tmp_path)

    status = asyncio.run(tf.handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-media", "originator": "alice"}}))

    assert status == {"status": "DROP"}, f"a deterministic media failure must DROP, not retry: {status}"

    fails = _fail_events(dapr, settings)
    assert len(fails) == 1, f"expected exactly one FAIL run recorded, got {len(fails)}"
    event = fails[0]
    assert event["outputs"][0]["name"] == "silver$derived"
    assert "cannot decode" in event["run"]["facets"]["errorMessage"]["message"]


def test_the_FAIL_run_names_the_person_the_media_was_derived_for(tmp_path: Any, underivable: None) -> None:
    """Trap 2 on the failure path — the only path where being told actually matters.

    The mover authors with a chart role literal, so the author facet reaches an inbox named after a
    ROLE. `lance.originator` is what carries the human, and a failed run that omits it is delivered to
    nobody while acking SUCCESS.
    """
    dapr, settings = _FakeDapr(), _settings(tmp_path)

    asyncio.run(tf.handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-media", "originator": "alice"}}))

    lance_facet = _fail_events(dapr, settings)[0]["run"]["facets"]["lance"]
    assert lance_facet.get("originator") == "alice", (
        f"the FAIL event dropped the originator, so the person who asked for this media is not reachable from it: {lance_facet}"
    )


def test_a_broken_compensating_control_is_logged_instead_of_swallowed(
    tmp_path: Any, underivable: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The suppression stays; the silence does not.

    Before `_best_effort`, a defect inside the FAIL emit was indistinguishable from a healthy DROP:
    `suppress(Exception)` discarded the exception and the handler returned DROP either way. That is the
    compensating control failing exactly as quietly as the thing it compensates for.
    """

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("outbox unreachable")

    monkeypatch.setattr(tf.outbox, "publish_lineage_with_outbox", _boom)
    dapr, settings = _FakeDapr(), _settings(tmp_path)

    with caplog.at_level(logging.ERROR):
        status = asyncio.run(tf.handle_stage(cast(DaprClient, dapr), settings, {"data": {"token": "tok-media", "originator": "alice"}}))

    assert status == {"status": "DROP"}, "a failed FAIL-emit must still DROP — that guarantee is the point"
    assert any("medallion_best_effort_emit_failed" in r.message for r in caplog.records), (
        "the compensating control failed and said nothing; that is the silence this helper removes"
    )
