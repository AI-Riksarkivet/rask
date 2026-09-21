"""Every lineage outcome carries WHICH DOOR the event came through.

ONE COUNTER, THREE DOORS, AND THE ALERT COULD NOT SAY WHICH. `lineage_events_processed_total` is
written by the Dapr subscriber, by the dead-letter handler and by the HTTP ingest endpoint — all three
in the SAME pod, so no instance or resource attribute separates them. An operator woken by
`lance_lineage_outcome="refused"` learned that provenance was lost and nothing about where to look,
and the three doors fail for different reasons and are fixed in different places: the subscriber's
refusals are a tuple the bus producer lacks, the HTTP door's are a tuple a SIDECAR-LESS producer lacks
(the Ray lane, every runner, any external OpenLineage producer), and a dead-letter park is the
resiliency schedule having given up.

MEASURED 2026-09-21, which is why this is a gate rather than a nicety. A refused dummy-lane ingest was
recorded correctly by the HTTP door and could not be found: `unrepairable` stood at 15,398 and rose by
3,656 in 47 seconds from the subscriber re-presenting one unauthored run, so a single real loss on
another door was arithmetically invisible. The fix was observable in a unit test and not in the
cluster, which is the wrong way round.

CARDINALITY IS STILL BOUNDED, which is the constraint `core/metrics.py` sets and this honours rather
than waives: `Door` is a closed StrEnum of three, so the series count triples at worst and no
per-run or per-table identifier is anywhere near a metric attribute.

REQUIRED, NOT DEFAULTED. A default would let a new call site silently inherit some other door's label,
which is the failure this exists to prevent — so `record_outcome` takes `door` as a keyword-only
argument with no default and a missing one is a TypeError at import-time coverage, not a wrong number.
"""

from __future__ import annotations

import pytest

from lineage.core.metrics import Door, Outcome


def test_every_door_is_distinct_and_bounded() -> None:
    values = [d.value for d in Door]

    assert len(values) == len(set(values)), f"two doors share a label value, so they would aggregate into one series: {values}"
    assert len(values) <= 4, f"{len(values)} doors — this attribute is only defensible while it stays a small closed set: {values}"


def test_the_door_reaches_the_metric_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driven through `record_outcome`, so a call that drops the attribute cannot pass."""
    from lineage.core import metrics

    seen: list[dict[str, str]] = []

    class _Counter:
        def add(self, _amount: int, attributes: dict[str, str]) -> None:
            seen.append(attributes)

    monkeypatch.setattr(metrics, "_events_processed", _Counter())
    metrics.record_outcome(Outcome.REFUSED, door=Door.HTTP)

    assert seen == [{"lance.lineage.outcome": "refused", "lance.lineage.door": "http"}]


def test_record_outcome_REFUSES_a_call_that_names_no_door() -> None:
    """The guard that makes the above true of every call site rather than of this one."""
    from lineage.core import metrics

    with pytest.raises(TypeError):
        metrics.record_outcome(Outcome.REFUSED)  # ty: ignore[missing-argument]
