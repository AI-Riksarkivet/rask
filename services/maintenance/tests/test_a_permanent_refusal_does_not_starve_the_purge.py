"""`due_from` ordering: a permanently-refused trash record must not hold a purge slot forever.

`trash_purge_max_per_tick` slices the due list before the per-record refusal checks run, so an
oldest-first order lets an unpurgeable record occupy the same slot on every tick. Measured on the
deployed estate 2026-09-20: 884 expired records, a cap of 25, 18 of them refused as "outside the
maintained estate" — 7 slots of real work and a queue that never advances.
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.services.purge import check, due_from


def _rec(rid: str, *, expires: str = "2026-01-01T00:00:00+00:00", attempts: Any = None) -> dict[str, Any]:
    record: dict[str, Any] = {"id": rid, "kind": "table", "expires_at": expires}
    if attempts is not None:
        record["attempts"] = attempts
    return record


def test_due_from_refused_record_sorts_after_never_tried() -> None:
    refused = _rec("stuck", expires="2026-01-01T00:00:00+00:00", attempts=57)
    fresh = _rec("fresh", expires="2026-06-01T00:00:00+00:00")

    assert [r["id"] for r in due_from([refused, fresh])] == ["fresh", "stuck"]


def test_due_from_refused_record_is_still_returned() -> None:
    # An order, not a filter — dropping it would hide the state that needs a human.
    assert {r["id"] for r in due_from([_rec("a", attempts=99), _rec("b")])} == {"a", "b"}


def test_due_from_equal_attempts_sorts_oldest_first() -> None:
    newer = _rec("newer", expires="2026-06-01T00:00:00+00:00")
    older = _rec("older", expires="2026-01-01T00:00:00+00:00")

    assert [r["id"] for r in due_from([newer, older])] == ["older", "newer"]


@pytest.mark.parametrize("attempts", [None, "seven", -1, 0])
def test_due_from_unreadable_attempts_sorts_as_never_tried(attempts: Any) -> None:
    # Fail toward attempting: one more refusal costs a tick, sorting it last costs the record.
    tried = _rec("tried", expires="2026-02-01T00:00:00+00:00", attempts=5)

    assert [r["id"] for r in due_from([_rec("bad", attempts=attempts), tried])] == ["bad", "tried"]


def test_check_outside_estate_reason_omits_the_root_list() -> None:
    reason = check({"id": "t", "kind": "table", "location": "s3://gone-wh/x"}, roots={f"s3://w{i}" for i in range(100)}, live_ids=set())

    assert reason is not None
    assert "s3://gone-wh/x" in reason
    assert "100 roots" in reason
    assert "s3://w1'" not in reason
