"""A promotion can be UNUSUAL without being broken, and nothing was looking.

The quality gate answers on assertions: a null key, an unresolvable blob pointer, a zero row count.
Those are corruption, and blocking them is right. But the retired plan `open_medallion_workflow.md` (its rulings now live in `docs/architecture/medallion-cascade.md`) §4 names the case
the archive actually has — "a row-count delta outside the expected band ... a first promotion of a
newly ingested volume" — and observes that today those are "either auto-promoted (if no assertion
covers them) or dropped forever (if one does). There is no third answer."

S4 built the third answer (hold -> ask -> resume) and S3 built the automatic split, but BOTH are
reached only from a failed assertion. A silver->gold promotion whose row count doubled passes every
assertion there is, so it never enters the hold path at all: it is promoted, silently, and the review
machinery that exists to catch exactly this never runs.

§9.1 decided the policy on 2026-08-15 — ±25%, plus first-promotion-of-a-dataset — and said the value
"lands WITH its consumer, in S3, or not at all", to avoid config nothing reads. S3 shipped. The band
did not. This is that half.

The split matters and is asserted below: a band breach is a QUESTION, never a verdict. Structural
failures block with nobody asked; an unusual delta must reach a person, because the whole point is
that only a human knows whether this volume really did double.
"""

from __future__ import annotations

import pytest

from medallion.services.promotion_band import FIRST_PROMOTION, ROW_DELTA, review_reasons


BAND = 0.25


class TestABreachIsFlagged:
    @pytest.mark.parametrize("current", [126, 74])
    def test_a_delta_outside_the_band_asks(self, current: int) -> None:
        assert ROW_DELTA in review_reasons(row_count=current, previous_row_count=100, band=BAND)

    def test_the_boundary_is_inclusive_so_exactly_the_band_is_normal(self) -> None:
        """±25% means 25% is still normal. An exclusive boundary makes the shipped intent 24.99%
        and turns the documented number into a lie."""
        assert review_reasons(row_count=125, previous_row_count=100, band=BAND) == []
        assert review_reasons(row_count=75, previous_row_count=100, band=BAND) == []


class TestTheFirstPromotionAlwaysAsks:
    """The clause that actually decides whether anyone ever looks at a new table — §9.1 says the
    band's exact width does not matter for this case, and this case is the one that does."""

    @pytest.mark.parametrize("previous", [0])
    def test_no_previous_version_asks(self, previous: int | None) -> None:
        assert FIRST_PROMOTION in review_reasons(row_count=500, previous_row_count=previous, band=BAND)

    def test_it_does_not_also_claim_a_delta_it_cannot_compute(self) -> None:
        """There is no previous count to compare against, so reporting a row-delta breach as well
        would put a reason in front of a person that is not a fact."""
        assert review_reasons(row_count=500, previous_row_count=None, band=BAND) == [FIRST_PROMOTION]


class TestTheBandIsAKnobAndFailsSAFE:
    def test_a_wider_band_asks_less(self) -> None:
        assert review_reasons(row_count=140, previous_row_count=100, band=0.25) != []
        assert review_reasons(row_count=140, previous_row_count=100, band=0.50) == []

    @pytest.mark.parametrize("band", [0.0])
    def test_a_nonsensical_band_asks_rather_than_waving_through(self, band: float) -> None:
        """A misconfigured band must not become a silent auto-promote. Asking too often is visible
        and annoying; asking never is invisible, which is the failure this whole slice exists to end."""
        assert ROW_DELTA in review_reasons(row_count=101, previous_row_count=100, band=band)


class TestAReasonIsNotAVerdict:
    def test_band_reasons_are_not_structural(self) -> None:
        """`resolve_review_policy` BLOCKS on a structural reason with nobody asked. A band breach
        routed into that set would silently turn "unusual" back into "dropped forever" — the exact
        behaviour §4 says is wrong."""
        from medallion.workflow import _STRUCTURAL_FAILURES

        assert ROW_DELTA not in _STRUCTURAL_FAILURES
        assert FIRST_PROMOTION not in _STRUCTURAL_FAILURES
