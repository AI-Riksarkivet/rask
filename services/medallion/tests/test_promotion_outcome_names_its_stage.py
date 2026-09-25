"""An approved promotion must be recorded as the STAGE that was held, not as the producer.

MEASURED live on the deployed estate, 2026-08-26. A silver->gold promotion was held by the quality
gate, approved through `POST /promotions/{id}/decision`, and landed in lineage as::

    job       lance-medallion/embed_features     <- the BRONZE->SILVER stage
    author    data_eng                           <- the BRONZE->SILVER author
    version   v1                                 <- the table was at v48
    outputs   ['acme-gold$catalog']              <- correct

`aggregate_gold` / `analyst` / v48 is the truth. So `acme-gold$catalog` reports that the silver stage
produced it, and the graph answers "who produced this gold table" with the wrong job, the wrong
author, and a version that never existed.

THE MECHANISM. `emit_promotion_outcome` runs in the PRODUCER, because the producer is what hosts the
`promotion_review` workflow and the door a person can reach. It builds its event with::

    operation=settings.operation      # producer's own settings
    author=settings.author            # producer's own settings
    # version: not passed at all -> build_run_event's `version: int = 1`

and the producer sets neither `MEDALLION_OPERATION` nor `MEDALLION_AUTHOR` (verified on the live
Deployment), so both fall to the code defaults in `core/config.py` -- `"embed_features"` and
`"data_eng"`. Those defaults are the bronze->silver stage runner's real values, which is exactly why this
hid: on a bronze->silver promotion the emit is ACCIDENTALLY correct, and only a second lane exposes it.

`PromotionSpec` already carries `from_dataset`/`to_dataset`/`version` -- which is why the inputs and
outputs on the same event ARE right -- but not the operation or the author, so the one activity that
knows which stage was held cannot say so.

WHY THIS IS THE SAME BUG THE FILE ALREADY FIXED ONCE. `hold_spec`'s docstring says `pub_topic`
"matters most -- the producer hosting the review has no idea what this stage runner's next hop is, so
without it an approval records a decision and promotes nothing." Identical reasoning, identical
carrier, and the operation/author/version were left behind.

NOT a notifications regression: `author` here is a chart ROLE LITERAL by design (see the
`rask-notifications` skill, trap 1 -- `enforce_author` would overwrite a human anyway), and
`originator` is what makes the run reachable. This moves the literal from the wrong stage's to the
right one's; it does not put a person in the author field.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
from dapr.ext.workflow import WorkflowActivityContext

from medallion.core.config import MedallionSettings, get_settings
from medallion.services.promotion_hold import hold_spec
from medallion.services.transform import StageIdentity, resolve_stage_identity
from medallion.workflow import PromotionOutcome, PromotionReport, emit_promotion_outcome


#: The producer's real deployed state: neither var is set, so `MedallionSettings` falls to the
#: bronze->silver defaults. Pinned explicitly so the test cannot pass because a stray env var in the
#: runner happened to name the gold stage — which is also why the fixture clears `get_settings`'s
#: cache: the activity reads the process-wide cached settings, so deleting the env alone pins nothing
#: once an earlier test has populated that cache.
_PRODUCER_ENV_IS_UNSET = ("MEDALLION_OPERATION", "MEDALLION_AUTHOR")

#: A tenant, because that is the shape a promotion has on the deployed estate: on a chart lane the
#: stage runner project-qualifies all four names in `resolve_stage_identity` BEFORE the hold is taken,
#: so the spec the producer receives already reads `acme-silver` / `acme-silver$features`.
_PROJECT = "acme"


def _bronze_to_silver_stage() -> MedallionSettings:
    """The bronze-to-silver stage runner's env as `chart/values.yaml` `medallion.stageRunners` sets it.

    Tenant-free, as in the chart: qualifying the names is production's job, not the test's.
    """
    return MedallionSettings(
        MEDALLION_FROM_NAMESPACE="bronze",
        MEDALLION_FROM_DATASET="bronze$events",
        MEDALLION_TO_NAMESPACE="silver",
        MEDALLION_TO_DATASET="silver$features",
        MEDALLION_OPERATION="embed_features",
        MEDALLION_AUTHOR="data_eng",
    )


def _silver_to_gold_stage() -> MedallionSettings:
    """The silver-to-gold stage runner's env as `chart/values.yaml` `medallion.stageRunners` sets it."""
    return MedallionSettings(
        MEDALLION_FROM_NAMESPACE="silver",
        MEDALLION_FROM_DATASET="silver$features",
        MEDALLION_TO_NAMESPACE="gold",
        MEDALLION_TO_DATASET="gold$catalog",
        MEDALLION_OPERATION="aggregate_gold",
        MEDALLION_AUTHOR="analyst",
    )


class _StubActivityContext:
    """An activity context. `emit_promotion_outcome` never touches it."""


def _ctx() -> WorkflowActivityContext:
    """The stub, typed as the real context: constructing one needs a live workflow instance."""
    return cast(WorkflowActivityContext, _StubActivityContext())


def _approved_hold(stage: MedallionSettings, *, version: int) -> tuple[StageIdentity, PromotionReport]:
    """An approved hold, composed on the path the stage runner takes in `transform._report_hold`.

    The stage resolves its four names with `resolve_stage_identity` and hands exactly those to
    `hold_spec`, so a hand-typed spec can only ever approximate them. The identity is returned beside
    the report because it is also what the held stage's own lineage names (`_emit_fail_run` reads the
    same four fields), and the outcome has to land on those nodes.
    """
    identity = resolve_stage_identity(stage, spec=None, project=_PROJECT)
    spec = hold_spec(
        stage,
        token="tok-hold",
        project=_PROJECT,
        from_namespace=identity.from_namespace,
        from_dataset=identity.from_dataset,
        to_namespace=identity.to_namespace,
        to_dataset=identity.to_dataset,
        reasons=["row_count_positive"],
        originator="alice",
        version=version,
    )
    return identity, PromotionReport(spec=spec, outcome=PromotionOutcome(status="PROMOTED", decided_by="alice"))


def _gold_hold() -> tuple[StageIdentity, PromotionReport]:
    """A silver->gold promotion held on v48, approved. The lane the producer's defaults do NOT describe."""
    return _approved_hold(_silver_to_gold_stage(), version=48)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Capture what the activity asks `build_run_event` for, and never touch Dapr.

    The activity imports `build_run_event` INSIDE the function body, so patching the module attribute
    is what the call actually resolves. `_run_async` is replaced with a closer rather than a no-op:
    a coroutine that is created and dropped raises `RuntimeWarning: never awaited`, which would make
    this test noisy for a reason unrelated to what it asserts.
    """
    calls: list[dict[str, Any]] = []

    def _spy(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"run": {"runId": "r1", "facets": {}}, "eventType": kwargs.get("event_type", "COMPLETE")}

    def _close(coro: Any) -> None:
        close = getattr(coro, "close", None)
        if callable(close):
            close()

    monkeypatch.setattr("medallion.schemas.events.build_run_event", _spy)
    monkeypatch.setattr("medallion.workflow._run_async", _close)
    for var in _PRODUCER_ENV_IS_UNSET:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield calls
    get_settings.cache_clear()


def test_the_approved_promotion_names_the_stage_that_was_held(captured: list[dict[str, Any]]) -> None:
    """The whole defect in one assertion set: job, author and version must describe the gold hop."""
    _, report = _gold_hold()
    emit_promotion_outcome(_ctx(), report)

    assert captured, "emit_promotion_outcome built no run event at all"
    event = captured[-1]

    assert event["operation"] == "aggregate_gold", (
        f"the gold promotion was recorded as job {event['operation']!r} -- lineage now says the silver stage produced acme-gold$catalog"
    )
    assert event["author"] == "analyst", f"the gold promotion was authored by {event['author']!r}, the bronze->silver stage runner's literal"
    assert event.get("version") == 48, (
        f"the promotion recorded version {event.get('version')!r}; the hold was taken on v48, and "
        "build_run_event's default of 1 claims a version the table never had at that point"
    )


def test_the_outputs_were_never_the_broken_half(captured: list[dict[str, Any]]) -> None:
    """Non-vacuity, and it pins the asymmetry that made this hard to see.

    Inputs and outputs come off the SPEC and were always right; only the fields read from `settings`
    were wrong. If a future refactor breaks these too, this test says so instead of the graph.

    A chart lane's spec arrives already project-qualified, so "right" means the SAME nodes the held
    stage's own lineage named: neither the producer's settings (`bronze` -> `silver`, tenant-free) nor
    a second qualification of a name that already carries the tenant (`acme-acme-gold$catalog`).
    """
    held, report = _gold_hold()
    emit_promotion_outcome(_ctx(), report)
    event = captured[-1]

    read, wrote = (held.from_namespace, held.from_dataset), (held.to_namespace, held.to_dataset)
    assert event["inputs"] == [read], f"the approval read {event['inputs']!r}; the held stage read {read!r}"
    assert (event["output_namespace"], event["output_name"]) == wrote, (
        f"the approval wrote {(event['output_namespace'], event['output_name'])!r}; the held stage wrote {wrote!r}"
    )


def test_a_silver_hold_still_names_the_silver_stage(captured: list[dict[str, Any]]) -> None:
    """The accidental-pass case, made deliberate.

    A bronze->silver promotion produced the RIGHT answer before this fix, for the wrong reason -- the
    producer's defaults happen to be that stage's values. Pinning it stops a fix that merely swaps one
    hardcoded stage for another from looking correct.
    """
    _, report = _approved_hold(_bronze_to_silver_stage(), version=72)

    emit_promotion_outcome(_ctx(), report)
    event = captured[-1]

    assert event["operation"] == "embed_features"
    assert event["author"] == "data_eng"
    assert event["version"] == 72


def test_a_hold_taken_before_this_change_still_emits(captured: list[dict[str, Any]]) -> None:
    """In-flight holds must not break, and the approval window is 72 HOURS.

    A `PromotionSpec` serialized into a running workflow's history before this field existed replays
    with `operation`/`author` empty. That is a real migration case -- the same one `publish_promotion`
    already handles for `version` -- so the emit falls back to settings rather than emitting an empty
    job name, which would be a worse record than the wrong one.
    """
    _, report = _gold_hold()
    report.spec.operation, report.spec.author, report.spec.version = "", "", 0

    emit_promotion_outcome(_ctx(), report)
    event = captured[-1]

    assert event["operation"] == "embed_features", "an old spec must fall back to settings, not emit an empty job name"
    assert event["author"] == "data_eng"
    assert event["version"] == 1, "no recorded version falls back to build_run_event's own default"
