"""A reported refusal carries the attempt count that decides when it is next tried.

[[LH-102]]. `due_from` orders the queue by `_drain_order` — attempts first, then `expires_at` — so a
record that has been refused before sorts behind one that has not. That is the whole fix for a queue
where 18 permanent refusals held 18 of the 25 per-tick slots forever.

The number driving that order was not in the one line an operator reads. `RefusedRecord` carries
`attempts`, the result line dropped it, and the question it answers is exactly the one asked when the
queue looks stuck: is this record being retried and demoted, or refused afresh every tick?

Measured on the first real purge (2026-09-20): `purged=7 refused=18 capped=859`, and every refused
entry in the log read `{'id': …, 'reason': …}` with no attempt count — so the reorder could be
inferred from the next tick's behaviour and from nothing in the tick that performed it.

`attempts` is None on a DRY RUN by construction and the test asserts that too: `_refuse` withholds
`trash.note_refusal` on a preview, because annotating the stuck records an operator is inspecting
would inflate the evidence the preview exists to show. A null there is the preview working.
"""

from __future__ import annotations

from maintenance.services.purge import RefusedRecord, refusal_log_entry


def test_a_recorded_refusal_reports_its_attempt_count() -> None:
    entry = refusal_log_entry(RefusedRecord(kind="table", id="ns$t", reason="outside the maintained estate", attempts=3))

    assert entry["attempts"] == 3, f"the attempt count that decides drain order is missing from the line an operator reads: {entry}"
    assert entry["id"] == "table:ns$t"


def test_a_preview_refusal_reports_no_attempt_count() -> None:
    """None is the preview working, not a gap: a dry run must not annotate the records it inspects."""
    entry = refusal_log_entry(RefusedRecord(kind="table", id="ns$t", reason="outside the maintained estate", attempts=None))

    assert entry["attempts"] is None
