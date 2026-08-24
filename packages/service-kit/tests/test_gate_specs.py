"""Quality-gate settings as a governed record, not a Deployment's env block.

The gate decides whether a stage's output may publish: which column identifies a row, which columns
a consumer depends on, and how far a row-count may move before a promotion needs a human. Every one
of those lived ONLY as env on a mover pod, so changing a threshold meant editing a values file and
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

from service_kit.lakehouse import gate_specs
from service_kit.lakehouse.gate_specs import GateSpec


@pytest.fixture
def root(tmp_path: object) -> str:
    return str(tmp_path)


def test_a_declared_gate_round_trips(root: str) -> None:
    spec = GateSpec(project="acme", key_column="id", required_columns=["id", "payload"], review_band=0.5, review_enabled=True)
    gate_specs.put_spec(root, {}, spec)

    got = gate_specs.get_spec(root, {}, "acme")

    assert got is not None
    assert got.review_band == 0.5
    assert got.required_columns == ["id", "payload"]


def test_an_undeclared_project_answers_none(root: str) -> None:
    """`None`, never a default — the medallion needs "unset" distinguishable from "set to 0"."""
    assert gate_specs.get_spec(root, {}, "never-declared") is None


def test_a_negative_band_is_refused() -> None:
    """A band is a magnitude. Negative would make every delta a breach, silently."""
    with pytest.raises(ValueError):
        GateSpec(project="acme", review_band=-0.1)


def test_two_projects_do_not_collide(root: str) -> None:
    gate_specs.put_spec(root, {}, GateSpec(project="acme", review_band=0.25))
    gate_specs.put_spec(root, {}, GateSpec(project="other", review_band=0.9))

    acme = gate_specs.get_spec(root, {}, "acme")
    other = gate_specs.get_spec(root, {}, "other")
    assert acme is not None and other is not None
    assert acme.review_band == 0.25
    assert other.review_band == 0.9
