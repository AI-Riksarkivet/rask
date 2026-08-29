"""With the catalog gating, the mover publishes and stops — the tag move is the trigger.

Two gates ran identical assertions in two places with different consequences. The catalog's withholds
the `published` TAG; the mover's withheld only the next TRIGGER, so a refused batch was already
committed into silver or gold and visible to anyone reading `latest` (`assert_quality_on_batch`
documents that hole itself). Only the tag is a boundary.

So the mover stops firing the next stage and asks the catalog to publish instead. The tag move emits
`table_published`, the publication head routes it to the lane that owns the source namespace, and the
cascade has ONE trigger and ONE gate.

OPT-IN. It needs a catalog the mover can reach and authenticate to, and declared lane routes; an
estate missing either would simply stop cascading. A migration seam is honest where a silent fallback
would not be.
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
from medallion.services.catalog_register import PublishOutcome


class _Dapr:
    def __init__(self) -> None:
        self.topics: list[str] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.topics.append(kwargs["topic_name"])


def _settings(tmp_path: Path, **over: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "bronze",
        "MEDALLION_FROM_DATASET": "bronze$events",
        "MEDALLION_TO_NAMESPACE": "silver",
        "MEDALLION_TO_DATASET": "silver$features",
        "MEDALLION_SUB_TOPIC": "medallion.bronze",
        "MEDALLION_PUB_TOPIC": "medallion.silver",
        "MEDALLION_CASCADE_VIA_PUBLISH": "true",
        "MEDALLION_CATALOG_URL": "http://catalog.test",
        # Publishing offers what this stage WROTE, so the writer must be on — the settings guard
        # refuses the combination at boot rather than letting the cascade stop quietly.
        "MEDALLION_COMPUTE_ENABLED": "true",
        "MEDALLION_FROM_URI": str(tmp_path / "bronze.lance"),
        "MEDALLION_TO_URI": str(tmp_path / "silver.lance"),
    }
    return MedallionSettings(**{**base, **over})


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """A real bronze dataset, so the compute path this feature depends on actually runs."""
    lance.write_dataset(pa.table({"id": [1, 2, 3]}), str(tmp_path / "bronze.lance"))
    return tmp_path


def _event() -> dict[str, Any]:
    return {"data": {"token": "tok", "dataset": "bronze$events", "namespace": "bronze"}}


@pytest.fixture
def published(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Capture the publish asks; the compute path stays the in-process one."""
    asks: list[dict[str, Any]] = []

    def _publish(**kwargs: Any) -> PublishOutcome:
        asks.append(kwargs)
        return PublishOutcome(published=True, from_version=1, to_version=2)

    monkeypatch.setattr(transform.catalog_register, "publish_stage_output", _publish)
    # The mover now ASKS the catalog where to write before writing. Without this the fixture's
    # catalog URL would make a real HTTP call; the stub hands back a path under tmp_path so the
    # compute still lands somewhere writable.
    monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", lambda **_: str(tmp_path / "vended.lance"))
    return asks


class TestTheTagMoveIsTheTrigger:
    def test_the_mover_publishes_instead_of_firing_the_next_stage(self, published: list[dict[str, Any]], upstream: Path) -> None:
        dapr = _Dapr()

        asyncio.run(transform.handle_stage(cast(Any, dapr), _settings(upstream), _event()))

        assert len(published) == 1, "the output was never offered to the catalog"
        assert "medallion.silver" not in dapr.topics, (
            "the mover fired the next-stage trigger AND published — two ignitions for one hop, which is the duplicate cascade this replaces"
        )

    def test_lineage_is_still_emitted(self, published: list[dict[str, Any]], upstream: Path) -> None:
        """The run's provenance does not depend on the gate's verdict — a refused batch still wrote."""
        dapr = _Dapr()

        asyncio.run(transform.handle_stage(cast(Any, dapr), _settings(upstream), _event()))

        assert "lineage.events.v1" in dapr.topics

    def test_the_declared_columns_reach_the_door(self, published: list[dict[str, Any]], upstream: Path) -> None:
        dapr = _Dapr()

        asyncio.run(transform.handle_stage(cast(Any, dapr), _settings(upstream, MEDALLION_REQUIRED_COLUMNS="id,embedding"), _event()))

        assert published[0]["required_columns"] == ["id", "embedding"]


class TestARefusalBecomesTheHold:
    def test_a_refused_publish_stops_the_cascade_and_names_its_assertions(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        holds: list[Any] = []

        monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", lambda **_: str(upstream / "vended.lance"))
        monkeypatch.setattr(
            transform.catalog_register,
            "publish_stage_output",
            lambda **_: PublishOutcome(published=False, failed_assertions=["row_count_positive"]),
        )
        monkeypatch.setattr(transform.promotion_hold, "review_enabled", lambda _s: True)
        monkeypatch.setattr(transform.promotion_hold, "hold_spec", lambda *a, **k: holds.append(k) or k)

        async def _publish_hold(_dapr: Any, _settings: Any, _spec: Any) -> bool:
            return True

        monkeypatch.setattr(transform.promotion_hold, "publish_hold", _publish_hold)
        dapr = _Dapr()

        status = asyncio.run(transform.handle_stage(cast(Any, dapr), _settings(upstream), _event()))

        assert status == {"status": "DROP"}
        assert "medallion.silver" not in dapr.topics
        assert holds and holds[0]["reasons"] == ["row_count_positive"], (
            "the catalog's verdict must reach the review — without the assertion names it cannot tell a corrupt finding from a reviewable one"
        )


class TestTheDefaultIsUntouched:
    def test_there_is_no_flag_and_no_second_door(self, monkeypatch: pytest.MonkeyPatch, upstream: Path) -> None:
        """THE MIGRATION IS OVER, so the seam is gone.

        This asserted the opposite: with MEDALLION_CASCADE_VIA_PUBLISH unset the mover fired
        `medallion.silver` itself and never called the catalog. That was honest as a MIGRATION SEAM,
        which is how this module's header describes it -- but it made the DEFAULT deployment promote
        through the door `publication.py` says must not exist, and left two enforcement points for one
        contract.

        The flag is removed, so the setting cannot turn the second door back on. A stage promotes
        through the catalog or it does not promote.
        """
        called: list[Any] = []
        monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", lambda **_: str(upstream / "vended.lance"))
        monkeypatch.setattr(transform.catalog_register, "publish_stage_output", lambda **k: called.append(k))
        dapr = _Dapr()

        asyncio.run(transform.handle_stage(cast(Any, dapr), _settings(upstream, MEDALLION_CASCADE_VIA_PUBLISH="false"), _event()))

        assert called, "the catalog must be asked to publish even with the retired flag set to false"
        assert "medallion.silver" not in dapr.topics, "the mover must never fire the next stage itself"
