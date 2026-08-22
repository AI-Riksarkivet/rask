"""A promotion can be UNUSUAL without being broken, and nothing was looking.

The quality gate answers on assertions: a null key, an unresolvable blob pointer, a zero row count.
Those are corruption, and blocking them is right. But `open_medallion_workflow.md` §4 names the case
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

import shutil
import subprocess
from pathlib import Path

import pytest
from medallion.services.promotion_band import FIRST_PROMOTION, ROW_DELTA, review_reasons


BAND = 0.25

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "chart"


def _render(*set_values: str) -> str:
    """`helm template` with the values the chart REFUSES to render without.

    Inlined rather than imported from `tests/unit/test_lineage_emission_wiring.py`, which has the same
    helper: pytest runs with `--import-mode=importlib` and explicit testpaths, so one suite cannot
    import another's module. Duplicating ten lines beats a cross-suite import that fails at collection.
    """
    helm = shutil.which("helm") or str(REPO / ".localbin/helm")
    if not Path(helm).exists():
        pytest.skip("helm not available")
    argv = [
        helm,
        "template",
        "rask",
        str(CHART),
        "--set-string",
        "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum",
        "--set-string",
        "frontend.oidc.publicIssuer=http://localhost:8080/dex",
        "--set-string",
        "frontend.oidc.publicOrigin=http://localhost:8080",
        "--set",
        "image.localImages=true",
    ]
    for value in set_values:
        argv += ["--set", value]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, f"helm template failed:\n{proc.stderr}"
    return proc.stdout


class TestABreachIsFlagged:
    @pytest.mark.parametrize("current", [126, 200, 1000, 74, 10, 0])
    def test_a_delta_outside_the_band_asks(self, current: int) -> None:
        assert ROW_DELTA in review_reasons(row_count=current, previous_row_count=100, band=BAND)

    @pytest.mark.parametrize("current", [100, 125, 75, 110, 90])
    def test_a_delta_inside_the_band_does_not(self, current: int) -> None:
        assert review_reasons(row_count=current, previous_row_count=100, band=BAND) == []

    def test_the_boundary_is_inclusive_so_exactly_the_band_is_normal(self) -> None:
        """±25% means 25% is still normal. An exclusive boundary makes the shipped intent 24.99%
        and turns the documented number into a lie."""
        assert review_reasons(row_count=125, previous_row_count=100, band=BAND) == []
        assert review_reasons(row_count=75, previous_row_count=100, band=BAND) == []


class TestTheFirstPromotionAlwaysAsks:
    """The clause that actually decides whether anyone ever looks at a new table — §9.1 says the
    band's exact width does not matter for this case, and this case is the one that does."""

    @pytest.mark.parametrize("previous", [0, None])
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

    @pytest.mark.parametrize("band", [0.0, -1.0])
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


class TestReadingThePredecessor:
    def test_the_first_version_has_no_predecessor(self) -> None:
        from medallion.services.promotion_band import previous_row_count

        assert previous_row_count("s3://nowhere/x.lance", {}, version=1) is None

    def test_an_unreadable_history_asks_rather_than_promoting(self) -> None:
        """A dataset whose previous version cannot be opened must reach a person. Defaulting to a
        number would make an unreadable predecessor indistinguishable from an unremarkable one."""
        from medallion.services.promotion_band import previous_row_count

        assert previous_row_count("s3://definitely-not-a-real-bucket/x.lance", {}, version=7) is None


class TestTheKnobIsReachable:
    """§9.1 refused to ship this value before its consumer existed, naming the defect precisely: "a
    `promotionReviewBand` value in `values.yaml` today would be config nothing reads — the dead-config
    defect this plane has already been bitten by twice (the orphan-scan lever that existed in
    `config.py` with no path from values, and an S1 state-store scope naming an app-id that does not
    exist)."

    A knob is only real when all three links hold: values declares it, the template renders it into
    the pod that USES it, and the settings field reads that exact env name. Any one missing and the
    number in values.yaml is decoration."""

    def test_values_declares_it(self) -> None:
        import yaml

        values = yaml.safe_load((REPO / "chart" / "values.yaml").read_text())
        assert "promotionReviewBand" in values["medallion"], "no value to set"

    def test_the_settings_field_reads_that_env_name(self) -> None:
        from medallion.core.config import MedallionSettings

        field = MedallionSettings.model_fields["promotion_review_band"]
        assert field.alias == "MEDALLION_PROMOTION_REVIEW_BAND"

    def test_the_MOVER_gets_it_rendered(self) -> None:
        """The MOVER, specifically. The band is evaluated at dispatch, where the write just happened —
        rendering it only onto the producer would be a value the deciding process never sees."""
        rendered = _render("medallion.quality=true", "medallion.qualityReview=true")
        assert "MEDALLION_PROMOTION_REVIEW_BAND" in rendered, "the band is not rendered under quality+qualityReview — the setting exists and no pod receives it"

    def test_it_is_not_rendered_when_review_is_off(self) -> None:
        """Not cosmetic: with review off there is nobody to ask, so the mover must not compute a
        band at all. Rendering it anyway would advertise a policy the deployment cannot honour."""
        rendered = _render("medallion.quality=true", "medallion.qualityReview=false")
        assert "MEDALLION_PROMOTION_REVIEW_BAND" not in rendered
