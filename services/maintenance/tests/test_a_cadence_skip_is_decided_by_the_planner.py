"""A dataset the policy says to skip is DECIDED at the planner, not published and skipped at a worker.

[[LH-191]]. `plan_sweep` returns `(items, decided)`. `decided` carries the trash exclusions; a policy
skip — `compact_enabled=False`, or a `compact_interval_hours` that has not elapsed — lands in
`plan.skipped` and the item is published anyway. So the tick enqueues every dataset in the estate
regardless of what any cadence says, and the skip happens a queue hop later.

MEASURED 2026-09-23 on the live planner: `planned=570 published=570 skipped=14
skipped_by={'trashed': 14}`, over `policies=27` — and the lane drains ~4.5 units/s against the 4.75/s
that injects, so it needs ~127s to clear a tick and gets 120. Every cadence-skipped unit is a message
published, delivered, and answered SUCCESS for doing nothing.

WITHHOLDING IS SAFE, AND THE CODE SAYS SO. A skipped plan does nothing at the worker by design:
`_stamp_policy_state` returns early on `plan.skipped is not None`, because "re-stamping there would
push the next maintenance out by another full interval on every tick — a dataset frozen forever by the
mechanism that exists to pace it". So the only effect of publishing one is the round trip.

THE ACCOUNTING MOVES WITH IT rather than disappearing: a withheld dataset becomes a `DatasetResult`
in `decided` carrying its own reason, which is what the trash exclusions already do — so the tick's
`skipped_by` reports the cadence alongside the trash instead of reporting only what the trash froze.
That is the row's closing bar: "a tick reports a non-zero cadence skip count".
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.services import sweep as sweep_module


@pytest.fixture
def planned(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Drive `plan_sweep` over three datasets: one maintainable, one paced out, one disabled."""
    skips = {
        "s3://wh/a_paced": "policy_interval",
        "s3://wh/a_disabled": "policy_disabled",
    }
    uris = ["s3://wh/a_live", "s3://wh/a_paced", "s3://wh/a_disabled"]

    monkeypatch.setattr(sweep_module, "_load_policies", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep_module, "_trash_exclusions", lambda *_a, **_k: {})
    monkeypatch.setattr(sweep_module, "_s3fs", lambda *_a, **_k: object())
    monkeypatch.setattr(sweep_module, "_buckets_to_sweep", lambda *_a, **_k: ["wh"])
    monkeypatch.setattr(sweep_module, "_discover_all", lambda *_a, **_k: list(uris))
    monkeypatch.setattr(sweep_module, "_protected_roots", lambda *_a, **_k: type("P", (), {"is_protected": staticmethod(lambda _u: None)})())
    monkeypatch.setattr(sweep_module, "_exclude_trashed", lambda u, _t: (list(u), []))
    # ORDER IS SHUFFLED inside `plan_sweep`, so the assertions below key on the uri, never on position.
    monkeypatch.setattr(
        sweep_module,
        "_resolve_plan",
        lambda uri, **_k: sweep_module.DatasetPlan(skipped=skips.get(uri), policy={"id": "p"} if uri in skips else None),
    )
    return sweep_module


def _settings() -> Any:
    from maintenance.core.config import MaintenanceSettings

    return MaintenanceSettings.model_validate({"MAINTENANCE_S3_BUCKET": "wh"})


def test_a_paced_dataset_is_not_enqueued(planned: Any) -> None:
    """The whole point: the cadence decision belongs where the plan is made."""
    items, _decided = planned.plan_sweep(_settings())
    assert [i.uri for i in items] == ["s3://wh/a_live"], f"a skipped dataset was still enqueued: {[i.uri for i in items]}"


def test_the_skip_keeps_its_reason_in_the_DECIDED_list(planned: Any) -> None:
    """Withholding must not lose the accounting — the tick's `skipped_by` is how anyone sees a cadence
    working at all, and the row's closing bar is a non-zero count there."""
    _items, decided = planned.plan_sweep(_settings())
    by_uri = {d.uri: d.skipped for d in decided}
    assert by_uri == {"s3://wh/a_paced": "policy_interval", "s3://wh/a_disabled": "policy_disabled"}, by_uri


def test_a_maintainable_dataset_is_still_enqueued(planned: Any) -> None:
    """The control. Without it every assertion above passes on a planner that enqueues nothing."""
    items, _decided = planned.plan_sweep(_settings())
    assert len(items) == 1 and items[0].plan.skipped is None
