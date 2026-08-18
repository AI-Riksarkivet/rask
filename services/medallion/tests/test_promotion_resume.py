"""An approved promotion resumes by MOVING THE TAG, not by firing a trigger nothing listens for.

Under a tag-driven cascade the next-stage trigger no longer exists — the tag move is what wakes the
next lane. So `publish_promotion` had to change with the cascade, and it could not simply call
`publish` again: the version still fails the same assertion it failed the first time, so an ordinary
re-publish would be refused for exactly the reason a person just overruled.

It publishes with the findings the review ACCEPTED. That is the door built for this, and it carries
its own limits: named findings only (a failure the approver never saw still refuses) and structural
findings never, by anyone.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from medallion.workflow import PromotionSpec


def _publishing_estate(monkeypatch: pytest.MonkeyPatch) -> None:
    """An estate whose cascade moves by publishing — the condition this resume path is for."""
    import medallion.core.config as config

    settings = config.MedallionSettings(
        MEDALLION_CASCADE_VIA_PUBLISH="true",
        MEDALLION_COMPUTE_ENABLED="true",
        MEDALLION_CATALOG_URL="http://catalog.test",
        MEDALLION_FROM_URI="/tmp/a.lance",
        MEDALLION_TO_URI="/tmp/b.lance",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)


def _spec(**over: Any) -> PromotionSpec:
    base: dict[str, Any] = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "silver",
        "from_dataset": "silver$features",
        "to_namespace": "gold",
        "to_dataset": "gold$catalog",
        "pub_topic": "",
        "reasons": ["row_count_positive"],
        "approver": "CiQwOGE4",
        "version": 7,
    }
    return PromotionSpec(**{**base, **over})


class TestTheResumeMovesTheTag:
    def test_it_publishes_the_version_the_hold_was_taken_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not "whatever is latest": a later commit may have landed while the approver was deciding,
        and publishing that one would ship a version nobody reviewed."""
        from medallion import workflow

        asks: list[dict[str, Any]] = []
        monkeypatch.setattr(workflow, "_resume_publish", lambda **k: asks.append(k))
        _publishing_estate(monkeypatch)

        workflow.publish_promotion(cast("Any", None), _spec().model_dump())

        assert asks and asks[0]["version"] == 7
        assert asks[0]["table_id"] == "gold$catalog"

    def test_it_carries_the_ACCEPTED_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without them the door re-runs the same assertions and refuses for the reason a person just
        overruled — the approval would be a no-op."""
        from medallion import workflow

        asks: list[dict[str, Any]] = []
        monkeypatch.setattr(workflow, "_resume_publish", lambda **k: asks.append(k))
        _publishing_estate(monkeypatch)

        workflow.publish_promotion(cast("Any", None), _spec(reasons=["row_count_positive", "column_declared"]).model_dump())

        assert sorted(asks[0]["accept_assertions"]) == ["column_declared", "row_count_positive"]


class TestTheSpecCarriesTheVersion:
    def test_a_hold_without_a_version_cannot_be_resumed(self) -> None:
        """The version is what makes the resume specific. A spec that never captured it would leave the
        approver's decision unattached to anything."""
        assert PromotionSpec.model_fields["version"].is_required() is False
        assert _spec().version == 7
