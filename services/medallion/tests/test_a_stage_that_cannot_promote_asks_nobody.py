"""A stage that cannot promote asks nobody to approve it — because approving it would open a second door.

`gate_decision` names one door: the stage promotes through the catalog's tag move, and nothing else may
advance a tier. `MISCONFIGURED` is the stage that has a catalog and a downstream but no publish target,
and `_PROMOTION_STATUS` already rules it out of the promotion verdicts: it is a deployment fault, and no
validator can act on one.

MEASURED at 05a7d3ec (`scratchpad/tb-promo/probe_second_door.py`): a stage that wrote nothing, with a
catalog, `MEDALLION_PUB_TOPIC=medallion.gold` and review on, published a hold with `version: 0` whose only
reason was the misconfiguration sentence; `resolve_review_policy` answered `review`, and approving it
fired `medallion.gold` from the producer with no catalog call at all. So a person's yes promoted a tier
the catalog never ruled on.

A hold is published only for a promotion VERDICT (HOLD, BLOCK, PUBLISH-refused), each of which
exists only because the stage wrote a version the catalog could be asked about.

Asking nobody is not the same as saying nothing: every refusal, MISCONFIGURED included, still leaves
its FAIL run in the graph, or the stage reads as a successful hop whose downstream never fired.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest
from dapr.aio.clients import DaprClient
from pydantic import BaseModel, Field

from medallion.core.config import MedallionSettings
from medallion.schemas.promotion import PromotionSpec
from medallion.services import transform
from medallion.services.compute import WriteResult
from medallion.services.gate_decision import GateOutcome, refusal_message
from medallion.services.transform import PromotionVerdict, StageIdentity, _report_hold, resolve_stage_identity
from medallion.services.trigger_guards import StageTrigger
from service_kit.lakehouse.transform_specs import TransformSpec


class _Bus:
    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish_event(self, **kwargs: Any) -> None:
        self.published.append(kwargs)

    def fail_runs(self, topic: str) -> list[dict[str, Any]]:
        """The FAIL RunEvents published on `topic`, decoded."""
        events = [json.loads(p["data"]) for p in self.published if p["topic_name"] == topic]
        return [event for event in events if event.get("eventType") == "FAIL"]


def _stage(**over: str) -> MedallionSettings:
    """A governed silver->gold stage runner with review on and a downstream topic."""
    base = {
        "MEDALLION_FROM_NAMESPACE": "silver",
        "MEDALLION_FROM_DATASET": "silver$features",
        "MEDALLION_TO_NAMESPACE": "gold",
        "MEDALLION_TO_DATASET": "gold$catalog",
        "MEDALLION_PUB_TOPIC": "medallion.gold",
        "MEDALLION_CATALOG_URL": "http://catalog.test",
        "MEDALLION_QUALITY_REVIEW_ENABLED": "true",
        "MEDALLION_QUALITY_REVIEW_APPROVER": "alice",
    }
    return MedallionSettings.model_validate(base | over)


def _wrote_nothing(monkeypatch: pytest.MonkeyPatch) -> tuple[MedallionSettings, _Bus, dict[str, str]]:
    """The measured path, end to end through the handler: compute off, so no version exists."""
    monkeypatch.setattr(transform.catalog_register, "describe_table_location", lambda **_: None)
    monkeypatch.setattr(transform.catalog_register, "ensure_stage_output", lambda **_: "/never/written.lance")
    settings, bus = _stage(), _Bus()

    ack = asyncio.run(transform.handle_stage(cast(DaprClient, bus), settings, {"data": {"token": "tok", "dataset": "silver$features", "namespace": "silver"}}))
    return settings, bus, ack


def test_a_stage_that_WROTE_NOTHING_publishes_no_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    settings, bus, ack = _wrote_nothing(monkeypatch)

    holds = [p for p in bus.published if p["topic_name"] == settings.promotion_topic]
    assert holds == [], f"a stage that cannot promote asked a person to approve it: {holds}"
    assert ack["status"] == "SUCCESS", f"the refusal is recorded, so it must not park: {ack}"


def test_a_stage_that_WROTE_NOTHING_still_leaves_its_FAIL_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph names the deployment fault, and files it under no promotion verdict: no validator can act on it."""
    settings, bus, _ = _wrote_nothing(monkeypatch)

    fails = bus.fail_runs(settings.lineage_topic)

    assert len(fails) == 1, f"a stage that can never promote left {len(fails)} FAIL runs on {settings.lineage_topic}"
    facets = fails[0]["run"]["facets"]
    assert facets["errorMessage"]["message"] == refusal_message(GateOutcome.MISCONFIGURED, settings.to_dataset)
    assert "promotion_status" not in facets["lance"], f"a deployment fault was filed as a promotion verdict: {facets['lance']}"


_IDENTITY = StageIdentity(from_namespace="acme-silver", from_dataset="acme-silver$features", to_namespace="acme-gold", to_dataset="acme-gold$catalog")

#: A lane declared through the catalog's door with tenant-free ids: the stage resolves exactly these names.
_DECLARED = TransformSpec(name="curate", project="acme", from_id="landing$events", to_id="curated$catalog", task="dummy")


class _FailureEmit(BaseModel):
    """One `_emit_stage_failure` call, as `_report_hold` made it."""

    identity: StageIdentity
    promotion_status: str | None


class _Reported(BaseModel):
    """What `_report_hold` published: the holds and the FAIL runs."""

    held: list[PromotionSpec] = Field(default_factory=list)
    failures: list[_FailureEmit] = Field(default_factory=list)


def _failure_recorder(reported: _Reported) -> Callable[..., Awaitable[None]]:
    """A stand-in for `_emit_stage_failure` with its whole signature, so a dropped argument cannot pass."""

    async def _emit_stage_failure(
        dapr: DaprClient,
        settings: MedallionSettings,
        identity: StageIdentity,
        trigger: StageTrigger,
        *,
        label: str,
        transition: str,
        project: str,
        token: str | None,
        error_message: str,
        promotion_status: str | None = None,
    ) -> None:
        reported.failures.append(_FailureEmit(identity=identity, promotion_status=promotion_status))

    return _emit_stage_failure


def _report(monkeypatch: pytest.MonkeyPatch, outcome: GateOutcome, identity: StageIdentity = _IDENTITY) -> _Reported:
    """Run `_report_hold` on a stage that WROTE v4, refused with `outcome`, and return what it published."""
    reported = _Reported()

    async def _publish_hold(_dapr: Any, _settings: Any, spec: PromotionSpec) -> bool:
        reported.held.append(spec)
        return True

    monkeypatch.setattr(transform.promotion_hold, "publish_hold", _publish_hold)
    monkeypatch.setattr(transform, "_emit_stage_failure", _failure_recorder(reported))
    asyncio.run(
        _report_hold(
            cast(DaprClient, _Bus()),
            _stage(),
            StageTrigger(token="tok", project="acme"),
            identity,
            PromotionVerdict(blocked=True, blocked_by=outcome, reasons=["row_count_positive"]),
            result=WriteResult(version=4, row_count=3, size_bytes=10),
            project="acme",
            transition="silver->gold",
            token="tok",
        )
    )
    return reported


def _names(spec: PromotionSpec) -> StageIdentity:
    return StageIdentity(from_namespace=spec.from_namespace, from_dataset=spec.from_dataset, to_namespace=spec.to_namespace, to_dataset=spec.to_dataset)


def _parameters(function: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    return [(p.name, p.kind, p.default) for p in inspect.signature(function).parameters.values()]


def test_the_FAIL_run_double_carries_the_real_signature() -> None:
    assert _parameters(_failure_recorder(_Reported())) == _parameters(transform._emit_stage_failure)


def test_a_MISCONFIGURED_stage_asks_nobody_even_when_it_wrote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing a version does not make a deployment fault a question: there is still no target the
    catalog could publish, so an approval has nothing it may do."""
    assert _report(monkeypatch, GateOutcome.MISCONFIGURED).held == []


@pytest.mark.parametrize("outcome", [GateOutcome.HOLD, GateOutcome.BLOCK, GateOutcome.PUBLISH])
def test_a_promotion_VERDICT_is_held_on_the_version_the_stage_wrote(monkeypatch: pytest.MonkeyPatch, outcome: GateOutcome) -> None:
    """The half that must survive: every verdict still reaches the review, pinned to the written version."""
    held = _report(monkeypatch, outcome).held

    assert [spec.version for spec in held] == [4], f"{outcome} held {held}"


@pytest.mark.parametrize("outcome", [GateOutcome.HOLD, GateOutcome.BLOCK, GateOutcome.PUBLISH])
def test_a_promotion_VERDICT_is_held_on_the_names_the_stage_resolved(monkeypatch: pytest.MonkeyPatch, outcome: GateOutcome) -> None:
    """The hold is the producer's only source of the four names, and it uses them as given, so the
    outcome, the approver's object and the catalog publish all name what this identity names."""
    held = _report(monkeypatch, outcome).held

    assert [_names(spec) for spec in held] == [_IDENTITY]


def test_a_DECLARED_lane_hold_carries_the_declared_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tenant-free declared ids stay tenant-free: re-qualified, they name tables the stage never wrote."""
    identity = resolve_stage_identity(_stage(), spec=_DECLARED, project="acme")

    held = _report(monkeypatch, GateOutcome.HOLD, identity).held

    assert [_names(spec) for spec in held] == [StageIdentity("landing", "landing$events", "curated", "curated$catalog")]


@pytest.mark.parametrize(
    ("outcome", "status"),
    [(GateOutcome.MISCONFIGURED, None), (GateOutcome.HOLD, "HELD"), (GateOutcome.BLOCK, "BLOCKED"), (GateOutcome.PUBLISH, "REFUSED")],
)
def test_every_refusal_leaves_one_FAIL_run_carrying_its_verdict(monkeypatch: pytest.MonkeyPatch, outcome: GateOutcome, status: str | None) -> None:
    """A question or not, the refusal is in the graph, on the stage's own names, under its own verdict."""
    failures = _report(monkeypatch, outcome).failures

    assert [(f.identity, f.promotion_status) for f in failures] == [(_IDENTITY, status)]
