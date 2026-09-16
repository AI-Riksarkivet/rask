"""A tick reports maintenance coverage per bucket, not only discovery coverage.

[[LH-101]]. `_discover_all` already logs `compaction_bucket_discovered` with a dataset count per
bucket, so an operator can see what the tick FOUND. Nothing said what it MAINTAINED, and the two differ
for reasons that matter: a refusal, a failure, or — since the per-tick budget landed — a pass that
stopped before reaching the bucket at all.

THE GAP IS THE SIGNAL. Discovered-minus-maintained per bucket is exactly the silent starvation this row
exists to end: with a budget set, the buckets at the tail of a truncated pass report zero maintained
while still reporting their discovered count, which is visible in a way "the tick finished" is not.

KEYED ON THE BUCKET because that is the unit an operator reasons about when a whole warehouse looks
stale. The dataset shuffle deliberately breaks bucket ordering, so a bucket's datasets are spread
through the pass — which means a truncated tick starves buckets PARTIALLY, and only a per-bucket count
shows it.
"""

from __future__ import annotations

from typing import Any

from maintenance.services import sweep as sweep_mod


def test_coverage_is_reported_per_bucket(caplog: Any) -> None:
    import logging

    planned = ["s3://a-wh/one", "s3://a-wh/two", "s3://b-wh/three"]
    maintained = ["s3://a-wh/one"]

    with caplog.at_level(logging.INFO, logger="maintenance.services.sweep"):
        sweep_mod.report_bucket_coverage(planned=planned, maintained=maintained)

    rows = {r.bucket: r for r in caplog.records if r.message == "compaction_bucket_maintained"}
    assert set(rows) == {"a-wh", "b-wh"}, f"expected one line per bucket, got {sorted(rows)}"
    assert (rows["a-wh"].planned, rows["a-wh"].maintained) == (2, 1)
    assert (rows["b-wh"].planned, rows["b-wh"].maintained) == (1, 0)


def test_a_bucket_the_tick_never_reached_still_reports(caplog: Any) -> None:
    """The whole point. A bucket that got nothing must appear with maintained=0, not be absent —
    absence reads as "no such bucket", which is what made the starvation invisible."""
    import logging

    with caplog.at_level(logging.INFO, logger="maintenance.services.sweep"):
        sweep_mod.report_bucket_coverage(planned=["s3://cold-wh/x", "s3://cold-wh/y"], maintained=[])

    rows = [r for r in caplog.records if r.message == "compaction_bucket_maintained"]
    assert len(rows) == 1
    assert (rows[0].bucket, rows[0].planned, rows[0].maintained) == ("cold-wh", 2, 0)


def test_nothing_planned_reports_nothing() -> None:
    """An empty tick must not invent a line — a report that fires with no work is noise that trains
    operators to ignore the one that matters."""
    sweep_mod.report_bucket_coverage(planned=[], maintained=[])
