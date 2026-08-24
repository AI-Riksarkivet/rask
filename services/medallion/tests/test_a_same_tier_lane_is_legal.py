"""A silver→silver derivation needs no code — and this pins the property that makes that true.

the retired plan `open_medallion_workflow.md` (its rulings now live in `docs/architecture/medallion-cascade.md`) filed S6 as "silver→silver derivations, once S1's shape has run in
anger", and §5's table reads "Not yet. Same shape as bronze→silver; adopt after that lands."

Read as outstanding WORK that is wrong, and worth stating plainly: the mover is a namespace-PAIR
machine. It reads `from_namespace/from_dataset`, writes `to_namespace/to_dataset`, and imposes no
ordering between them — there is no tier ladder in the code, no "silver must follow bronze" check,
nothing that inspects whether the two sides differ. A same-tier derivation is therefore a `movers[]`
entry in values.yaml (`fromNamespace: silver` → `toNamespace: silver`), not a feature.

That generality is the multimodal design working as intended: the tiers are exactly bronze→silver→gold
(R23), and a derivation WITHIN silver — enrich, fan out, add feature columns — crosses no tier at all.
Confusing the two is easy and expensive, because "add a fourth tier" is a governance change while "add
a lane" is a config line.

So what S6 actually needs is a test, not an implementation: nothing today forbids a same-tier lane,
and nothing today notices if a future tier guard starts to. A gate that asserted
`from_tier < to_tier` would look like a correctness improvement and would silently outlaw every
derivation lane in the estate.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest
from medallion.core.config import MedallionSettings
from medallion.services import transform


class _Dapr:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.topics.append(kwargs["topic_name"])


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), str(tmp_path / "silver.lance"))
    return tmp_path


def _same_tier_settings(tmp_path: Path) -> MedallionSettings:
    """The lane S6 describes: one silver table deriving another, no tier crossed."""
    env: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "silver",
        "MEDALLION_FROM_DATASET": "silver$features",
        "MEDALLION_TO_NAMESPACE": "silver",
        "MEDALLION_TO_DATASET": "silver$enriched",
        "MEDALLION_PUB_TOPIC": "medallion.silver.enriched",
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_FROM_URI": str(tmp_path / "silver.lance"),
        "MEDALLION_TO_URI": str(tmp_path / "enriched.lance"),
        # REQUIRED since the second door was removed: publishing is the only way to promote, and it
        # needs a catalog. Every mover the chart renders has one; a lane without one is the ungoverned
        # mode, which writes and never promotes.
        "MEDALLION_CATALOG_URL": "http://catalog.invalid",
    }
    return MedallionSettings(**env)


def _stub_catalog(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> list[dict[str, object]]:
    """Stand in for the catalog, which is now the ONLY door a stage may promote through.

    Before the second enforcement point was removed these tests needed no catalog: the mover fired
    the next stage's topic itself. It cannot any more, so a stage promotes only by publishing — hence
    both this stub AND `MEDALLION_CATALOG_URL` on the settings, because `gate_decision` will not
    choose PUBLISH without a catalog and the stub would then never be called.

    A stage with no catalog does NOT retry. It acks and does not promote (`GateOutcome.UNGOVERNED`),
    because redelivery cannot set an env var — this docstring said "RETRYs" while that was true of an
    intermediate state, and it was corrected rather than left standing. Same pattern as
    `test_cascade_via_publish.py`.
    """
    published: list[dict[str, object]] = []
    monkeypatch.setattr(transform.catalog_register, "register_stage_output", lambda **_: None)
    # The lane's REAL output URI. Returning a path the lane never writes makes the predecessor
    # unreadable and the stage RETRY -- a stub that lies about the vended location tests nothing.
    monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", lambda **_: str(upstream / "enriched.lance"))
    monkeypatch.setattr(transform.catalog_register, "publish_stage_output", lambda **k: published.append(k))
    return published


class TestTheMoverImposesNoTierLadder:
    def test_a_silver_to_silver_lane_runs(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, upstream: Path) -> None:
        _stub_catalog(monkeypatch, upstream)
        dapr = _Dapr()

        result = asyncio.run(transform.handle_stage(cast("Any", dapr), _same_tier_settings(upstream), {"data": {"token": "t"}}))

        # NOT `== SUCCESS`, and the difference is the point. That assertion passed only because the
        # mover used to fire the next topic itself, bypassing the gate. With one door the lane reaches
        # the gate like any other, and a destination with no predecessor is a FIRST PROMOTION -- a band
        # reason, so it HOLDs for a person ("a destination we cannot read is given a person's attention
        # rather than a silent promote", compute.py:272). What this test exists to prove is that a
        # same-tier lane is not refused FOR BEING SAME-TIER, and a drop would say so by name.
        assert result != {"status": "DROP"}, f"a same-tier derivation was refused as another lane's: {result}"
        assert "medallion_stage_other_lane" not in caplog.text

    def test_it_really_derived_a_second_dataset(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """Not merely "did not error" — the point of a derivation lane is a new governed table."""
        _stub_catalog(monkeypatch, upstream)
        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _same_tier_settings(upstream), {"data": {"token": "t"}}))

        assert (upstream / "enriched.lance").exists()
        assert lance.dataset(str(upstream / "enriched.lance")).count_rows() == 3

    def test_it_feeds_its_own_downstream(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """A derivation is a cascade hop like any other, so it must be able to feed the next one —
        otherwise a chain of derivations would need something outside the mover to drive it.

        THE MECHANISM CHANGED, THE PROPERTY DID NOT. This asserted the mover published
        `medallion.silver.enriched` itself. That was the SECOND enforcement point: promoting without
        the catalog ruling. The hop is now fed by the catalog's tag move, which emits
        `table_published` for the publication head to route — so what must be asserted is that the
        stage PUBLISHED, not that the mover fired a topic.
        """
        published = _stub_catalog(monkeypatch, upstream)
        dapr = _Dapr()

        asyncio.run(transform.handle_stage(cast("Any", dapr), _same_tier_settings(upstream), {"data": {"token": "t"}}))

        assert published, "a derivation must still feed its downstream — through the catalog's tag move"
        assert dapr.topics == [] or "medallion.silver.enriched" not in dapr.topics, "the mover must not fire the next stage itself; that is the second door"


class TestTheTiersThemselvesAreStillThree:
    def test_a_derivation_adds_no_governed_tier(self) -> None:
        """The guard against reading this test as licence for a fourth tier. R23: the medallion is
        exactly bronze→silver→gold, and a same-tier lane must not widen that vocabulary."""
        from service_kit.lakehouse.warehouse_registry import GOVERNED_TIERS

        assert set(GOVERNED_TIERS) == {"bronze", "silver", "gold"}

    def test_both_sides_of_the_lane_are_the_same_tier(self) -> None:
        from service_kit.lakehouse.warehouse_registry import namespace_tiers

        assert namespace_tiers("silver") == namespace_tiers("silver") == frozenset({"silver"})
