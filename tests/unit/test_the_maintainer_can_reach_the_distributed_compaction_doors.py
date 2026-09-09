"""`compaction_plan` and `compaction_commit` are MAINTENANCE doors, so a maintainer may reach them.

E2 bootstrapped the sweep as a `maintainer` rather than a `writer` — a deliberate narrowing, recorded
in `chart/templates/bootstrap-admin.yaml` and asserted by its own render gate. Distributed compaction
then calls two catalog routes the sweep alone uses, `compaction_plan` and `compaction_commit`, and
NEITHER is named in `fga_deps`' rung maps — so `_action_relation` falls them to the WRITER rung, which
a maintainer does not hold. The two rules are individually right and jointly deny.

MEASURED ON THE LIVE ESTATE 2026-09-09, and the shape is why it stayed invisible:

  * `maintenance.distributedCompaction` is `True` in the running pod, and **7,920 of 7,920** dataset
    outcomes in a 40-minute window report `mode: in_pod` — the feature is configured on and takes the
    fallback path every single time;
  * the catalog audits **~5,200 `can_write_data` DENY per hour** for `service-maintenance` against
    bronze tables across every tenant, with no `tier` attribute, so they come from the ROUTER gate and
    not from the vending door;
  * and none of it is red. The sweep compacts in-pod and reports success, so the only symptom is a
    memory ceiling that is a function of the largest table anyone owns — which is the exact thing the
    feature exists to remove.

This is the same defect class as `changes` and `history` (both fixed 2026-09-08/09 after the live audit
trail showed a read door demanding `can_write_data`), arriving from the other side: a MAINTENANCE door
demanding a rung the estate deliberately took away.
"""

from __future__ import annotations

import pytest

from catalog.api.fga_deps import _action_relation


@pytest.mark.parametrize("action", ["compaction_plan", "compaction_commit"])
def test_the_compaction_doors_ask_for_the_MAINTAINER_rung(action: str) -> None:
    """Not the writer rung. E2 took `writer` away from the sweep on purpose; this is what it kept."""
    relation = _action_relation("table", action)
    assert relation == "can_maintain", (
        f"{action} resolves to {relation!r} — the sweep holds `maintainer`, not `writer`, so the router "
        "denies before the endpoint runs and distributed compaction silently falls back to in-pod"
    )


def test_a_real_data_write_still_asks_for_the_WRITER_rung() -> None:
    """The narrowing must not leak: `insert` is a data write and a maintainer is not a writer."""
    assert _action_relation("table", "insert") == "can_write_data"


def test_an_unknown_suffix_still_falls_to_the_writer_rung() -> None:
    """Fail-closed stays fail-closed — the fix is naming these two doors, not widening the default."""
    assert _action_relation("table", "some_action_nobody_has_written_yet") == "can_write_data"
