"""Quality-gate settings as a governed record, not a Deployment's env block.

The gate decides whether a stage's output may publish: which column identifies a row, which columns
a consumer depends on, and how far a row-count may move before a promotion needs a human. Every one
of those lived ONLY as env on a stage runner pod, so changing a threshold meant editing a values file and
running `helm upgrade` — and nothing could list what the gates were, review one, or gate who
changed it.

Same stateless-over-object-store shape as `transform_specs`, and for the same reason: the catalog
WRITES (admin-gated, audited) and the medallion READS (on a path that holds no catalog client).
One format, defined once, rather than two copies that drift.

KEYED BY PROJECT, not by lane. `promotion_review_band` and `quality_key_column` are tenant-level
thresholds in the chart today, and making them per-lane here would invent a granularity the estate
does not have. A per-lane override is an added key, not a redesign.

UNSET IS NOT ZERO. `get_spec` answers `None` when nobody declared, exactly like the lane registry —
the medallion must be able to tell "not configured" (keep the chart's settings, byte-for-byte) from
"configured to 0.0" (every promotion breaches the band). Collapsing those two would silently put an
estate that never opted in under a new scheme.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.gate_specs import GateSpec


def test_a_negative_band_is_refused() -> None:
    """A band is a magnitude. Negative would make every delta a breach, silently."""
    with pytest.raises(ValueError):
        GateSpec(project="acme", review_band=-0.1)
