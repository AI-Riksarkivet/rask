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

import inspect
from collections.abc import Callable

from lineage.api.v1.endpoints import columns, datasets
from lineage.services import repository
from lineage.services.cypher import MAX_COLUMN_DEPTH, MAX_WALK_DEPTH, bounded_walk


def _depth_param(func: Callable[..., object]) -> object:
    return inspect.signature(func).parameters.get("depth")


class TestEveryWalkDoorOffersADepth:
    """The door is where a caller can express the bound — a repository parameter nobody can reach is not one."""

    def test_the_dataset_walks(self) -> None:
        for door in (datasets.get_upstream, datasets.get_downstream, datasets.get_graph):
            assert _depth_param(door) is not None, f"{door.__name__} declares no depth, so its bound is unreachable from the API"

    def test_the_column_walks(self) -> None:
        for door in (columns.get_column_upstream, columns.get_column_downstream):
            assert _depth_param(door) is not None, f"{door.__name__} declares no depth, so the column plane has no ceiling"


class TestTheRepositoryActuallyBoundsTheStatement:
    """A `depth` argument the query ignores is the shape that made `/upstream` look bounded for months."""

    def test_every_walk_method_takes_a_depth(self) -> None:
        for name in ("upstream", "downstream", "column_upstream", "column_downstream"):
            method = getattr(repository.LineageRepository, name)
            assert "depth" in inspect.signature(method).parameters, f"LineageRepository.{name} cannot be bounded at all"

    def test_the_column_methods_pass_it_through_bounded_walk(self) -> None:
        """Pinned on the SOURCE because the alternative is a live AGE connection.

        The check is narrow on purpose — that each column method names `bounded_walk` — because the
        defect was that they named the raw constant instead, which no signature or type can catch.
        """
        for name in ("column_upstream", "column_downstream"):
            source = inspect.getsource(getattr(repository.LineageRepository, name))
            assert "bounded_walk(" in source, f"LineageRepository.{name} still hands the raw statement to fetch, so its depth argument is inert"


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

    def test_a_bool_is_not_a_depth(self) -> None:
        """`bool` is an `int` subclass, so `True` would otherwise pass as depth 1."""
        import pytest

        from lineage.services import cypher as cy

        with pytest.raises(TypeError, match="must be an int"):
            bounded_walk(cy.UPSTREAM, True)

    def test_none_stays_unbounded(self) -> None:
        """The default every one of these doors keeps — truncating provenance silently is the worse failure."""
        from lineage.services import cypher as cy

        assert bounded_walk(cy.UPSTREAM, None) == cy.UPSTREAM
        assert bounded_walk(cy.COL_DOWNSTREAM, None) == cy.COL_DOWNSTREAM

    def test_the_column_ceiling_is_tighter_than_the_dataset_one(self) -> None:
        """Column lineage fans out per FIELD, so it reaches a bigger result set in fewer hops."""
        assert MAX_COLUMN_DEPTH < MAX_WALK_DEPTH
