"""A silver→silver derivation needs no code — and this pins the property that makes that true.

the retired plan `open_medallion_workflow.md` (its rulings now live in `docs/architecture/medallion-cascade.md`) filed S6 as "silver→silver derivations, once S1's shape has run in
anger", and §5's table reads "Not yet. Same shape as bronze→silver; adopt after that lands."

Read as outstanding WORK that is wrong, and worth stating plainly: the stage runner is a namespace-PAIR
machine. It reads `from_namespace/from_dataset`, writes `to_namespace/to_dataset`, and imposes no
ordering between them — there is no tier ladder in the code, no "silver must follow bronze" check,
nothing that inspects whether the two sides differ. A same-tier derivation is therefore a `stageRunners[]`
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
import inspect
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
import pytest

from medallion.core.config import MedallionSettings
from medallion.services import catalog_register, transform
from medallion.services.catalog_register import PublishOutcome


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
        # needs a catalog. Every stage runner the chart renders has one; a lane without one is the ungoverned
        # mode, which writes and never promotes.
        "MEDALLION_CATALOG_URL": "http://catalog.invalid",
        # Pinned, not inherited: with review on, a first promotion is a band reason and HOLDs, which
        # would fail the promotion assertions below for a reason unrelated to tiers.
        "MEDALLION_QUALITY_REVIEW_ENABLED": "false",
    }
    return MedallionSettings(**env)


def _stub_catalog(monkeypatch: pytest.MonkeyPatch, upstream: Path) -> list[dict[str, object]]:
    """Answer as the catalog client answers for a lane whose two tables it governs; return the publish asks.

    The catalog is the only door a stage promotes through, which is why the settings carry
    `MEDALLION_CATALOG_URL`: `gate_decision` does not choose PUBLISH without one, and a stage with no
    catalog acks UNGOVERNED without ever reaching this stub.

    Every answer is one the real client can give: a stand-in answering what production never does
    certifies a run production never has. `publish_stage_output` returns a `PublishOutcome` — a `None`
    crashes the handler after its write into RETRY. A FIRST publication has no prior published version,
    so `from_version` is ``None`` and `to_version` is the version asked about (the catalog's
    `PublicationResult`), and a `gate_only` probe never moves the tag. The locations are the lane's
    real ones, because a vended path the lane never writes makes the predecessor unreadable. Each
    double binds its call to the real function's signature, so a call the real client would refuse
    with a `TypeError` fails here too.
    """
    published: list[dict[str, object]] = []
    real_publish = catalog_register.publish_stage_output
    real_ensure = catalog_register.ensure_stage_output
    real_describe = catalog_register.describe_table_location

    def _publish(**kwargs: Any) -> PublishOutcome:
        inspect.signature(real_publish).bind(**kwargs)
        published.append(kwargs)
        return PublishOutcome(published=not kwargs.get("gate_only", False), from_version=None, to_version=kwargs["version"])

    def _ensure(**kwargs: Any) -> str:
        inspect.signature(real_ensure).bind(**kwargs)
        return str(upstream / "enriched.lance")

    def _describe(**kwargs: Any) -> str:
        inspect.signature(real_describe).bind(**kwargs)
        return str(upstream / "silver.lance")

    monkeypatch.setattr(catalog_register, "ensure_stage_output", _ensure)
    monkeypatch.setattr(catalog_register, "describe_table_location", _describe)
    monkeypatch.setattr(catalog_register, "publish_stage_output", _publish)
    return published


class TestTheMoverImposesNoTierLadder:
    def test_a_silver_to_silver_lane_runs(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, upstream: Path) -> None:
        published = _stub_catalog(monkeypatch, upstream)
        dapr = _Dapr()

        result = asyncio.run(transform.handle_stage(cast("Any", dapr), _same_tier_settings(upstream), {"data": {"token": "t"}}))

        # The whole dict, not its status: a HOLD also acks SUCCESS, with `reason: quality_blocked`, and a
        # tier guard that withheld promotion from same-tier lanes would pass a status-only check. Review
        # is off in this lane, so a first promotion raises no band reason and publishes.
        assert result == {"status": "SUCCESS"}, f"a same-tier derivation did not ack SUCCESS: {result}"
        # And the SUCCESS is a promotion: a lane that acked without publishing would pass the line above.
        promotions = [ask for ask in published if not ask.get("gate_only", False)]
        assert [ask["table_id"] for ask in promotions] == ["silver$enriched"], f"a same-tier derivation did not promote: {published}"
        assert "medallion_stage_other_lane" not in caplog.text

    def test_it_really_derived_a_second_dataset(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """Not merely "did not error" — the point of a derivation lane is a new governed table."""
        _stub_catalog(monkeypatch, upstream)
        asyncio.run(transform.handle_stage(cast("Any", _Dapr()), _same_tier_settings(upstream), {"data": {"token": "t"}}))

        assert (upstream / "enriched.lance").exists()
        assert lance.dataset(str(upstream / "enriched.lance")).count_rows() == 3

    def test_it_feeds_its_own_downstream(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """A derivation is a cascade hop like any other, so it must be able to feed the next one —
        otherwise a chain of derivations would need something outside the stage runner to drive it.

        THE MECHANISM CHANGED, THE PROPERTY DID NOT. This asserted the stage runner published
        `medallion.silver.enriched` itself. That was the SECOND enforcement point: promoting without
        the catalog ruling. The hop is now fed by the catalog's tag move, which emits
        `table_published` for the publication head to route — so what must be asserted is that the
        stage PUBLISHED, not that the stage runner fired a topic.
        """
        published = _stub_catalog(monkeypatch, upstream)
        dapr = _Dapr()

        asyncio.run(transform.handle_stage(cast("Any", dapr), _same_tier_settings(upstream), {"data": {"token": "t"}}))

        assert published, "a derivation must still feed its downstream — through the catalog's tag move"
        assert dapr.topics == [] or "medallion.silver.enriched" not in dapr.topics, (
            "the stage runner must not fire the next stage itself; that is the second door"
        )


class TestTheTiersThemselvesAreStillThree:
    def test_a_derivation_adds_no_governed_tier(self) -> None:
        """The guard against reading this test as licence for a fourth tier. R23: the medallion is
        exactly bronze→silver→gold, and a same-tier lane must not widen that vocabulary."""
        from service_kit.lakehouse.warehouse_registry import GOVERNED_TIERS

        assert set(GOVERNED_TIERS) == {"bronze", "silver", "gold"}

    def test_both_sides_of_the_lane_are_the_same_tier(self) -> None:
        from service_kit.lakehouse.warehouse_registry import namespace_tiers

        assert namespace_tiers("silver") == namespace_tiers("silver") == frozenset({"silver"})
