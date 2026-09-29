"""No operation in this service may be one unreadable body (MAINT-09).

`run_sweep` and `compact_one` had grown to 115 and 75 statements at six and five levels of nesting,
each doing in one body what the module's own docstrings describe as a sequence of distinct phases:
discovery, two protective registry reads, a whole-estate pre-pass, trash exclusion, per-dataset policy
resolution, tracing, the blocking Lance/S3 work and the metric aggregation. Every other function in the
package sits at 36 statements or fewer and at most three levels deep, so the two were outliers rather
than the shape of the problem — and they are the two whose failure modes delete data.

Thresholds are set just above the rest of the package: they exist to stop these two from growing back,
not to police code that was never the problem.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import sweep
from maintenance.services.optimize import DatasetResult
from service_kit.lakehouse import maintenance_policies


# --------------------------------------------------------------------------- #
# The invariants the extraction had to carry OUT of `run_sweep`'s body, pinned
# --------------------------------------------------------------------------- #


def test_a_cadence_skip_does_not_restamp_the_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `policy_interval` skip must NOT record a fresh `last_maintained_at`.

    Inside the old god function this was implicit: the skip `continue`d past the stamp block. Split into
    `_resolve_plan` + `_stamp_cadence` the skip still carries the policy record that produced it, so the
    stamp's own conditions (`policy` present, `compact_interval_hours` set, no error, no refusal) are ALL
    satisfied — and a stamp there pushes the next maintenance out by another full interval every tick,
    freezing the dataset forever behind the mechanism that exists to pace it.
    """
    written: list[str] = []

    def _write(*a: object, **_kw: object) -> None:
        written.append(str(a))

    monkeypatch.setattr(maintenance_policies, "write_state", _write)
    settings = MaintenanceSettings(s3_bucket="b", s3_secret_access_key=SecretStr("unit"))
    policy = {"id": "p1", "compact_interval_hours": 6}
    now = datetime.now(UTC)

    skipped = sweep.DatasetPlan(skipped="policy_interval", policy=policy)
    sweep._stamp_cadence("s3://b/t.lance", skipped, DatasetResult(uri="s3://b/t.lance", skipped="policy_interval"), settings=settings, options={}, now=now)
    assert written == [], "a cadence skip re-stamped the dataset — its next maintenance just moved another interval out"

    maintained = sweep.DatasetPlan(policy=policy)
    sweep._stamp_cadence("s3://b/t.lance", maintained, DatasetResult(uri="s3://b/t.lance", fragments_removed=1), settings=settings, options={}, now=now)
    assert len(written) == 1, "a real maintenance pass must still stamp, or the cadence never advances"
