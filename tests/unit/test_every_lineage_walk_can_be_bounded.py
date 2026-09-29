"""Every variable-length lineage walk can be bounded by the caller, not just two of them.

[[LH-006]]. `cypher.bounded_walk` and the ceilings `MAX_WALK_DEPTH` (20) / `MAX_COLUMN_DEPTH` (5) have
existed for a while, and two doors used them — `/datasets/{name}/graph` and the column-graph walk. Four
statements did not, in two different ways:

  * `/upstream` and `/downstream` never DECLARED a `depth`, so although `repository.upstream/downstream`
    have always accepted one and applied `bounded_walk`, every request through the door passed `None`
    and reached the unbounded `*1..` statement. The bound existed and was unreachable.
  * `column_upstream`/`column_downstream` took no `depth` at all and handed `cy.COL_UPSTREAM` /
    `cy.COL_DOWNSTREAM` to `fetch` raw, so the column plane had no ceiling to reach — not a default to
    override, no parameter to pass.

WHY IT MATTERS HERE RATHER THAN AS TIDINESS: `age.py` names the unbounded walk over a grown graph as
the reason a pooled connection cannot be pinned, and an unbounded correlated walk against the live
lineage graph OOM-killed the AGE container once already (2026-09-15, restart 0->1, recovered). Column
lineage is the walk most able to multiply, because it fans out per FIELD rather than per dataset.

THE DEFAULT IS UNCHANGED AND THAT IS DELIBERATE. Omitted, every one of these still walks unbounded —
the previous behaviour, and what an un-rooted caller wants. What changed is that bounding is now a
caller's CHOICE on all six walks instead of on two; silently truncating a provenance answer would be a
worse failure than a slow one, and it is not this row's ask.
"""

from __future__ import annotations

from lineage.services.cypher import MAX_WALK_DEPTH, bounded_walk


class TestTheCeilingsAreEnforcedNotAdvisory:
    """`bounded_walk` refuses rather than clamps, and the reason is in its own docstring: the hop range
    is SYNTAX interpolated into the query, so a value that is not a small positive integer is an
    injection vector. Clamping would run a query the caller never asked for and hide that they tried."""

    def test_a_depth_over_the_ceiling_is_refused(self) -> None:
        import pytest

        from lineage.services import cypher as cy

        with pytest.raises(ValueError, match="between 1 and"):
            bounded_walk(cy.UPSTREAM, MAX_WALK_DEPTH + 1)
        with pytest.raises(ValueError, match="between 1 and"):
            bounded_walk(cy.COL_UPSTREAM, 0)
