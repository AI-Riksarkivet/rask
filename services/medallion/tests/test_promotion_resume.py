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

import asyncio
from typing import Any, cast

import pytest
from pydantic import ValidationError

from medallion.schemas.promotion import PromotionSpec


def _publishing_estate(monkeypatch: pytest.MonkeyPatch) -> None:
    """An estate whose cascade moves by publishing — the condition this resume path is for."""
    import medallion.core.config as config

    settings = config.MedallionSettings(
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
        "operation": "aggregate_gold",
        "author": "analyst",
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

        workflow.publish_promotion(cast("Any", None), _spec())

        assert asks and asks[0]["version"] == 7
        assert asks[0]["table_id"] == "gold$catalog"

    def test_it_carries_the_ACCEPTED_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without them the door re-runs the same assertions and refuses for the reason a person just
        overruled — the approval would be a no-op."""
        from medallion import workflow

        asks: list[dict[str, Any]] = []
        monkeypatch.setattr(workflow, "_resume_publish", lambda **k: asks.append(k))
        _publishing_estate(monkeypatch)

        workflow.publish_promotion(cast("Any", None), _spec(reasons=["row_count_positive", "column_declared"]))

        assert sorted(asks[0]["accept_assertions"]) == ["column_declared", "row_count_positive"]


class TestTheSpecCarriesTheVersion:
    def test_a_hold_without_a_version_cannot_be_resumed(self) -> None:
        """The version is what makes the resume specific. A spec that never captured it would leave the
        approver's decision unattached to anything."""
        assert PromotionSpec.model_fields["version"].is_required() is True
        assert _spec().version == 7


class TestTheCatalogIsTheOnlyDoorAnApprovalHas:
    """`gate_decision`: the stage promotes through the catalog, which is the ONLY door. An approval is
    a promotion too, so it gets the same single door — a version the catalog is asked to publish."""

    def test_a_hold_naming_no_written_version_is_refused_at_the_hold_topic(self) -> None:
        """Lance numbers a dataset's first version 1 (measured on pylance 12.0.0, an empty create
        included), so 0 names nothing the catalog could publish. Refused where the hold arrives — DROP,
        because redelivery cannot add a version — and no review is ever opened for it."""
        from medallion.api.promotions import handle_promotion_held

        scheduled: list[Any] = []

        class _Engine:
            def schedule_new_workflow(self, *, workflow: Any, input: Any, instance_id: str) -> str:  # noqa: A002
                scheduled.append(input)
                return instance_id

            def raise_workflow_event(self, instance_id: str, event_name: str, *, data: Any = None) -> None:
                raise AssertionError("the hold ingress raises no event")

            def get_workflow_state(self, instance_id: str, *, fetch_payloads: bool = True) -> Any:
                return None

        ack = asyncio.run(handle_promotion_held({"data": _spec().model_dump() | {"version": 0}}, client=_Engine()))

        assert ack == {"status": "DROP"}
        assert scheduled == [], "a review was opened for a hold that names no written version"

    def test_an_approval_never_fires_a_topic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A valid approval moves the tag once and publishes nothing: a topic fired beside the catalog
        would advance a tier the catalog never ruled on."""
        import dapr.aio.clients

        import service_kit.dapr_publish
        from medallion import workflow

        resumed: list[dict[str, Any]] = []
        published: list[str] = []

        def _record_loop(coro: Any) -> None:
            published.append(f"_run_async({coro.__qualname__})")
            coro.close()

        async def _record_publish(*_a: Any, **kwargs: Any) -> None:
            published.append(f"publish to {kwargs.get('topic_name')}")

        class _RecordingDaprClient:
            def __init__(self, *_a: Any, **_k: Any) -> None:
                published.append("DaprClient()")

        monkeypatch.setattr(workflow, "_resume_publish", lambda **k: resumed.append(k))
        monkeypatch.setattr(workflow, "_run_async", _record_loop)
        monkeypatch.setattr(service_kit.dapr_publish, "publish_event", _record_publish)
        monkeypatch.setattr(service_kit.dapr_publish, "publish_json", _record_publish)
        monkeypatch.setattr(dapr.aio.clients, "DaprClient", _RecordingDaprClient)
        _publishing_estate(monkeypatch)

        workflow.publish_promotion(cast("Any", None), _spec())

        assert published == [], f"an approval published beside the catalog: {published}"
        assert [ask["version"] for ask in resumed] == [7], f"the approval asked the catalog {len(resumed)} times"

    def test_a_decoded_input_naming_no_written_version_is_refused_before_any_publish(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The engine hands the activity its DECODED input. One that names no written version — the
        shape a stage that wrote nothing produced, carrying a downstream topic — is refused before it
        can reach any publish, rather than resuming by firing that topic from the producer."""
        from medallion import workflow

        resumed: list[dict[str, Any]] = []
        fired: list[Any] = []
        monkeypatch.setattr(workflow, "_resume_publish", lambda **k: resumed.append(k))
        monkeypatch.setattr(workflow, "_run_async", lambda coro: fired.append(coro.close()))
        _publishing_estate(monkeypatch)
        decoded = _spec().model_dump() | {"version": 0, "pub_topic": "medallion.gold"}

        with pytest.raises(ValidationError):
            workflow.publish_promotion(cast("Any", None), cast("Any", decoded))

        assert fired == [], "an approval published to a topic: a promotion the catalog never ruled on"
        assert resumed == [], "an approval asked the catalog to publish a version that does not exist"
