"""Quality-gate settings as a governed record, not a Deployment's env block.

The gate decides whether a stage's output may publish: which column identifies a row, which columns
a consumer depends on, and how far a row-count may move before a promotion needs a human. Held as env on a pod,
changing a threshold would mean editing a values file and running `helm upgrade` — and nothing could
list what the gates were, review one, or gate who changed it.

Stateless over the object store: the catalog WRITES (admin-gated, audited) and its publish door READS.
One format, defined once, rather than two copies that drift.

KEYED BY PROJECT, not by lane. `promotion_review_band` and `quality_key_column` are tenant-level
thresholds, and making them per-lane here would invent a granularity the estate
does not have. A per-lane override is an added key, not a redesign.

UNSET IS NOT ZERO. `get_spec` answers `None` when nobody declared, so a reader can tell "not configured" from
"configured to 0.0" (every promotion breaches the band). Collapsing those two would silently put an
estate that never opted in under a new scheme.
"""

from __future__ import annotations

import pytest

from lakehouse_kit.gate_specs import GateSpec


def test_a_negative_band_is_refused() -> None:
    """A band is a magnitude. Negative would make every delta a breach, silently."""
    with pytest.raises(ValueError):
        GateSpec(project="acme", review_band=-0.1)
