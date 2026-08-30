"""A duplicated pass 2 over-reported the cascade's volume by a whole stage's output.

`publish_stage_ready` can run twice -- an activity retry, or ordinary at-least-once redelivery of
`sub_topic` -- and pass 2 re-runs to COMPLETION, because a same-version re-publish is accepted by
`publication.publish`. So `record_stage_completion` fired twice and `medallion.stage.rows` /
`medallion.stage.bytes` counted the same write again.

An idempotency key on the publish would not have fixed it: nothing on that path dedupes by key. The
correction belongs at the consumer, where it also covers plain redelivery -- which is the finding's
own reasoning, and why this is keyed on the token rather than on anything the activity carries.

VOLUME IS DEDUPED AND LATENCY IS NOT, deliberately. Rows and bytes are cumulative, so a second add is
a lie; a duration is a histogram observation, and a second sample of real work done is not.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.core import metrics


@pytest.fixture(autouse=True)
def _clean() -> Any:
    metrics._counted_volume.clear()
    yield
    metrics._counted_volume.clear()


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[int]]:
    seen: dict[str, list[int]] = {"rows": [], "bytes": [], "duration": []}

    class _Counter:
        def __init__(self, bucket: str) -> None:
            self._bucket = bucket

        def add(self, value: int, _attrs: Any = None) -> None:
            seen[self._bucket].append(value)

        def record(self, value: float, _attrs: Any = None) -> None:
            seen[self._bucket].append(int(value))

    monkeypatch.setattr(metrics, "_stage_rows", _Counter("rows"))
    monkeypatch.setattr(metrics, "_stage_bytes", _Counter("bytes"))
    monkeypatch.setattr(metrics, "_stage_duration", _Counter("duration"))
    return seen


def test_a_REPLAYED_stage_counts_its_volume_ONCE(counted: dict[str, list[int]]) -> None:
    """THE WEDGE. Both calls are the same batch making the same hop."""
    for _ in range(2):
        metrics.record_stage_completion("bronze->silver", duration_seconds=1.0, rows=100, size_bytes=2048, volume_key="bronze->silver:tok-1")

    assert counted["rows"] == [100], f"a redelivered stage added its rows twice: {counted['rows']}"
    assert counted["bytes"] == [2048]


def test_the_LATENCY_is_still_recorded_both_times(counted: dict[str, list[int]]) -> None:
    """The asymmetry, asserted rather than assumed: a duration is an observation, not a running total,
    and dropping the second sample would hide real work that really took that long."""
    for _ in range(2):
        metrics.record_stage_completion("bronze->silver", duration_seconds=1.0, rows=100, size_bytes=2048, volume_key="bronze->silver:tok-1")

    assert counted["duration"] == [1, 1]


def test_a_DIFFERENT_batch_is_counted_normally(counted: dict[str, list[int]]) -> None:
    """The guard must not make the counter sticky: two batches through the same hop are two results."""
    metrics.record_stage_completion("bronze->silver", duration_seconds=1.0, rows=100, volume_key="bronze->silver:tok-1")
    metrics.record_stage_completion("bronze->silver", duration_seconds=1.0, rows=7, volume_key="bronze->silver:tok-2")

    assert counted["rows"] == [100, 7]


def test_the_SAME_batch_through_a_DIFFERENT_hop_is_counted(counted: dict[str, list[int]]) -> None:
    """One batch crosses several tiers, and each hop moved its own rows."""
    metrics.record_stage_completion("bronze->silver", duration_seconds=1.0, rows=100, volume_key="bronze->silver:tok-1")
    metrics.record_stage_completion("silver->gold", duration_seconds=1.0, rows=100, volume_key="silver->gold:tok-1")

    assert counted["rows"] == [100, 100]


def test_a_caller_with_NO_key_is_unchanged(counted: dict[str, list[int]]) -> None:
    """The parameter is optional, so a caller that cannot name a stable key still counts -- the guard
    is an improvement where a key exists, never a new way to lose a measurement."""
    for _ in range(2):
        metrics.record_stage_completion("bronze->silver", duration_seconds=1.0, rows=5)

    assert counted["rows"] == [5, 5]


def test_the_dedupe_set_is_BOUNDED() -> None:
    """A metrics guard, not a ledger: an unbounded set in a long-lived mover is a leak, and the
    duplicates worth catching arrive seconds apart."""
    for i in range(metrics._COUNTED_VOLUME_MAX + 50):
        metrics.record_stage_completion("t", duration_seconds=0.0, rows=1, volume_key=f"t:{i}")

    assert len(metrics._counted_volume) <= metrics._COUNTED_VOLUME_MAX
