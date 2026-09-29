"""Per-bucket storage accounting, rolled up from the orphan scan's own listing ([[LH-074]]).

The scan already lists every file under a dataset prefix with its size — that is how it finds orphans
at all — and then discards the referenced ones. Summing the same pass is free, and it is the TOTAL
rather than the orphan subset: rolling up `OrphanFile.size_bytes` would measure unreferenced bytes,
which is not what a quota is about.
"""

from __future__ import annotations

from maintenance.services.reconcile import _roll_up


def test_roll_up_sums_datasets_sharing_a_bucket() -> None:
    assert _roll_up({"s3://wh-a/ds1": 100, "s3://wh-a/ds2": 50}) == {"wh-a": 150}
