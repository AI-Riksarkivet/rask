"""`/producers` reads one dataset's whole WROTE history, and one dataset's history is not bounded.

Run RETENTION (30 days, owner ruling 2026-09-08) bounds the GRAPH. It does not bound a single
dataset: a cascade tier rewritten every 120 s accrues ~21,600 WROTE edges inside that window, and this
endpoint returned all of them. Same shape as the run board before `list_runs_page` — measured there at
5,122 rows / 2.65 MB on an endpoint polled every two seconds.

The order is already newest-first for an unrelated and load-bearing reason (a consumer taking "the
latest run" must not read a stale earlier verdict), so bounding composes with it: what a caller loses
is the tail, never the current answer.
"""

from __future__ import annotations

import pytest

from lineage.services import cypher as cy


def test_the_query_carries_its_bound() -> None:
    assert cy.producers_page(5).endswith("LIMIT 5")
    assert "ORDER BY r.event_time DESC" in cy.producers_page(5), "the bound must keep the newest, not an arbitrary page"


@pytest.mark.parametrize("bad", [0, -1, cy.MAX_PRODUCERS_FETCH + 1])
def test_a_limit_outside_the_range_is_refused_before_interpolation(bad: int) -> None:
    """The value is interpolated as a LITERAL — AGE does not bind `$param` reliably outside a MATCH —
    so the range check is what stands between a caller's value and raw SQL inside AGE's `$$` quoting.
    """
    with pytest.raises(ValueError, match="producers limit must be between"):
        cy.producers_page(bad)


def test_a_bool_is_not_an_int_here() -> None:
    """`isinstance(True, int)` is True in Python, so a bool would sail through a range check and
    interpolate as `LIMIT True`. `list_runs_page` guards the same way."""
    with pytest.raises(TypeError, match="must be an int"):
        cy.producers_page(True)
