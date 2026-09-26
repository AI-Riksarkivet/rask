"""The stage runner's half: a HOLD becomes a question on the bus, or stays a verdict.

`_QUALITY_BLOCKED` is a permanent DROP, and it is the right answer for a corrupt blob pointer. It is
the wrong answer for a promotion that is merely UNUSUAL — a batch that legitimately shipped zero rows,
a declared column a consumer agreed to drop. Those are decisions, and a service cannot make them.

What a stage runner must NOT do is decide which it is. It publishes what it saw; the review workflow — which
runs in the producer, beside the door a person can reach — splits corrupt from unusual.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.schemas.promotion import PromotionSpec
from medallion.services.promotion_hold import hold_spec, publish_hold


class _Dapr:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[dict[str, Any]] = []
        self._fail = fail

    async def publish_event(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("broker unreachable")
        self.published.append(kwargs)


def _settings(**over: Any) -> MedallionSettings:
    base: dict[str, Any] = {
        "MEDALLION_FROM_NAMESPACE": "silver",
        "MEDALLION_FROM_DATASET": "silver$features",
        "MEDALLION_TO_NAMESPACE": "gold",
        "MEDALLION_TO_DATASET": "gold$catalog",
        "MEDALLION_QUALITY_REVIEW_ENABLED": "true",
        "MEDALLION_QUALITY_REVIEW_APPROVER": "CiQwOGE4",
        "MEDALLION_QUALITY_REVIEW_HOURS": "48",
    }
    return MedallionSettings(**{**base, **over})


class TestWhatTheHoldCarries:
    def test_the_spec_names_the_FAILED_assertions_only(self) -> None:
        """The review splits corrupt from unusual by reading these names, so a passing assertion in
        the list would block a promotion nothing is wrong with."""
        spec = hold_spec(
            _settings(),
            token="tok-1",
            project="acme",
            from_namespace="silver",
            from_dataset="silver$features",
            to_namespace="gold",
            to_dataset="gold$catalog",
            reasons=["row_count_positive"],
            originator="alice",
            version=7,
        )

        assert spec.reasons == ["row_count_positive"]

    def test_the_deadline_and_approver_are_read_at_DISPATCH(self) -> None:
        """Both ride the spec because a workflow body that read settings would replay against whatever
        the value is now, not what it was when the promotion was held."""
        spec = hold_spec(
            _settings(),
            token="tok-1",
            project="acme",
            from_namespace="silver",
            from_dataset="silver$features",
            to_namespace="gold",
            to_dataset="gold$catalog",
            reasons=["row_count_positive"],
            originator="",
            version=7,
        )

        assert spec.approval_hours == 48
        assert spec.approver == "CiQwOGE4"

    def test_the_hold_carries_NO_downstream_topic(self) -> None:
        """An approval resumes by asking the catalog to publish the held version, and the tag move wakes
        the next lane. A topic on the hold would hand the producer a way to wake it directly — the
        second door `gate_decision` rules out — so the stage runner's downstream never leaves it."""
        spec = hold_spec(
            _settings(MEDALLION_PUB_TOPIC="medallion.gold"),
            token="tok-1",
            project="acme",
            from_namespace="silver",
            from_dataset="silver$features",
            to_namespace="gold",
            to_dataset="gold$catalog",
            reasons=["row_count_positive"],
            originator="",
            version=7,
        )

        assert "medallion.gold" not in spec.model_dump_json(), f"the hold carries the downstream topic: {spec.model_dump()}"


class TestPublishingTheHold:
    @pytest.mark.asyncio
    async def test_it_goes_to_the_promotion_topic(self) -> None:
        dapr, settings = _Dapr(), _settings()
        spec = hold_spec(
            settings,
            token="tok-1",
            project="acme",
            from_namespace="silver",
            from_dataset="silver$features",
            to_namespace="gold",
            to_dataset="gold$catalog",
            reasons=["row_count_positive"],
            originator="",
            version=7,
        )

        assert await publish_hold(dapr, settings, spec) is True
        assert dapr.published[0]["topic_name"] == "medallion.promotion"
        # The whole spec, not one field: the hold topic refuses a hold missing any required one.
        assert PromotionSpec.model_validate(json.loads(dapr.published[0]["data"])) == spec

    @pytest.mark.asyncio
    async def test_a_LOST_publish_reports_false_rather_than_raising(self) -> None:
        """The caller has already written the output and emitted the held-run lineage. A broker blip
        must degrade to the old permanent BLOCK — which is safe — not unwind a completed write."""
        dapr, settings = _Dapr(fail=True), _settings()
        spec = hold_spec(
            settings,
            token="tok-1",
            project="",
            from_namespace="silver",
            from_dataset="silver$features",
            to_namespace="gold",
            to_dataset="gold$catalog",
            reasons=["row_count_positive"],
            originator="",
            version=7,
        )

        assert await publish_hold(dapr, settings, spec) is False


class TestReviewIsOptIn:
    def test_review_off_means_there_is_nothing_to_dispatch(self) -> None:
        """An estate with nobody to ask must keep the permanent BLOCK. Parking promotions on an
        external event no one will ever raise is worse than refusing them."""
        from medallion.services.promotion_hold import review_enabled

        assert review_enabled(_settings(MEDALLION_QUALITY_REVIEW_ENABLED="false")) is False

    def test_review_on_with_NO_approver_is_still_dispatched(self) -> None:
        """Deliberate: the workflow answers 'no reachable approver' in the outcome and in lineage. A
        stage runner that silently swallowed it would leave the operator with the same unexplained DROP the
        review exists to replace."""
        from medallion.services.promotion_hold import review_enabled

        assert review_enabled(_settings(MEDALLION_QUALITY_REVIEW_APPROVER="")) is True
