"""The tick's skip count says WHICH kind of skip, because two unrelated mechanisms share the number.

[[LH-191]]. The planner emits `maintenance_tick_enqueued ... planned=570 skipped=7` every 120s, and
those 7 are trash exclusions — but the summary cannot say so. The same integer counts three things
that mean opposite operational stories: a dataset in the trash prefix (correctly excluded, permanent),
a policy with `compact_enabled: false` (deliberately opted out), and a policy whose
`compact_interval_hours` has not elapsed (maintained recently — the CADENCE working).

THE ROW'S OWN CLOSING CONDITION IS THIS NUMBER. LH-191 closes when "a tick reports a non-zero cadence
skip count", and today that is unobservable even if it happened: all 27 registered policies carry
`compact_interval_hours: null`, so when somebody sets one, the only evidence that it took effect would
be `skipped` moving from 7 to 8 — indistinguishable from one more dataset reaching the trash.

ATTRIBUTION, NOT A SECOND COUNTER. `skipped` stays the total, because an alert reads it; `skipped_by`
carries the breakdown beside it. The reasons are already on the results — `DatasetResult.skipped` is
the literal `_policy_skip_reason` returned and `.trashed` is the trash reason — so this reports what
the planner already decided rather than deciding anything new.
"""

from __future__ import annotations

from typing import cast

import pytest

from maintenance.api import routes
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.services.optimize import DatasetResult


class _Publisher:
    async def publish_event(self, **kwargs: object) -> None:
        return None


class _Emitter:
    def emit_maintenance(self, *args: object, **kwargs: object) -> None:
        return None


class _S:
    work_topic = "maintenance.work.v1"
    work_pubsub = "maintenance-pubsub"
    publish_timeout_seconds = 5.0
    delimiter = "$"


async def _summary(monkeypatch: pytest.MonkeyPatch, decided: list[DatasetResult]) -> dict[str, object]:
    monkeypatch.setattr(routes, "plan_sweep", lambda settings: ([], decided))
    monkeypatch.setattr(routes, "record_run", lambda: None)

    async def _no_units(*args: object, **kwargs: object) -> tuple[int, list[object]]:
        return 0, []

    async def _no_lineage(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "enqueue_units", _no_units)
    monkeypatch.setattr(routes, "emit_sweep_lineage", _no_lineage)
    return await routes.on_cron(cast(MaintenanceSettings, _S()), cast(MaintenanceEmitter, _Emitter()), _Publisher())


#: What the live estate reports every tick today: trash exclusions only, and no cadence anywhere.
_TODAY = [DatasetResult(uri=f"s3://b/trashed{i}.lance", trashed="under _trash/") for i in range(7)]
#: What one tick looks like the moment somebody sets `compact_interval_hours` on a policy.
_WITH_CADENCE = [*_TODAY, DatasetResult(uri="s3://b/recent.lance", skipped="policy_interval")]


@pytest.mark.anyio
async def test_the_cadence_skip_is_countable_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """The leg [[LH-191]] closes on: a cadence skip must be visible as a cadence skip."""
    summary = await _summary(monkeypatch, _WITH_CADENCE)
    by_reason = summary.get("skipped_by")
    assert isinstance(by_reason, dict), f"the tick summary carries no skip attribution: {summary}"
    assert by_reason.get("policy_interval") == 1, (
        f"a dataset skipped for CADENCE is not counted as one ({by_reason}) — so the only evidence that "
        "an interval took effect is the total moving by one, which a dataset reaching the trash does too"
    )
    assert by_reason.get("trashed") == 7, f"the trash exclusions lost their own count: {by_reason}"


@pytest.mark.anyio
async def test_the_total_still_means_what_it_meant(monkeypatch: pytest.MonkeyPatch) -> None:
    """`skipped` is read by an alert. Attribution goes BESIDE it, never in place of it."""
    summary = await _summary(monkeypatch, _WITH_CADENCE)
    assert summary["skipped"] == 8, summary
    by_reason = summary["skipped_by"]
    assert isinstance(by_reason, dict)
    assert sum(by_reason.values()) == summary["skipped"], (
        f"the breakdown {by_reason} does not add up to the total {summary['skipped']} — a skip with a "
        "reason nobody anticipated must still be counted, or the two numbers disagree in production"
    )


@pytest.mark.anyio
async def test_todays_estate_reports_zero_cadence_skips_rather_than_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live tick, reproduced: seven trash exclusions and not one cadence skip. An ABSENT key and a
    ZERO read the same in a log line, so the key that is not there is the one an operator misreads."""
    summary = await _summary(monkeypatch, _TODAY)
    assert summary["skipped_by"] == {"trashed": 7}, summary
