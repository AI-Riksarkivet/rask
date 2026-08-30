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
`"data_eng"`. Those defaults are the bronze->silver mover's real values, which is exactly why this
hid: on a bronze->silver promotion the emit is ACCIDENTALLY correct, and only a second lane exposes it.

`PromotionSpec` already carries `from_dataset`/`to_dataset`/`version` -- which is why the inputs and
outputs on the same event ARE right -- but not the operation or the author, so the one activity that
knows which stage was held cannot say so.

WHY THIS IS THE SAME BUG THE FILE ALREADY FIXED ONCE. `hold_spec`'s docstring says `pub_topic`
"matters most -- the producer hosting the review has no idea what this mover's next hop is, so
without it an approval records a decision and promotes nothing." Identical reasoning, identical
carrier, and the operation/author/version were left behind.

NOT a notifications regression: `author` here is a chart ROLE LITERAL by design (see the
`rask-notifications` skill, trap 1 -- `enforce_author` would overwrite a human anyway), and
`originator` is what makes the run reachable. This moves the literal from the wrong stage's to the
right one's; it does not put a person in the author field.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.workflow import PromotionOutcome, PromotionReport, PromotionSpec, emit_promotion_outcome


#: The producer's real deployed state: neither var is set, so `MedallionSettings` falls to the
#: bronze->silver defaults. Pinned explicitly so the test cannot pass because a stray env var in the
#: runner happened to name the gold stage.
_PRODUCER_ENV_IS_UNSET = ("MEDALLION_OPERATION", "MEDALLION_AUTHOR")


def _gold_hold() -> PromotionReport:
    """A silver->gold promotion, approved. The lane the producer's defaults do NOT describe."""
    return PromotionReport(
        spec=PromotionSpec(
            token="tok-gold",
            project="acme",
            from_namespace="silver",
            from_dataset="silver$features",
            to_namespace="gold",
            to_dataset="gold$catalog",
            operation="aggregate_gold",
            author="analyst",
            version=48,
        ),
        outcome=PromotionOutcome(status="PROMOTED", decided_by="alice"),
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
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
    return calls


def test_the_approved_promotion_names_the_stage_that_was_held(captured: list[dict[str, Any]]) -> None:
    """The whole defect in one assertion set: job, author and version must describe the gold hop."""
    emit_promotion_outcome(None, _gold_hold())  # ty: ignore[invalid-argument-type]

    assert captured, "emit_promotion_outcome built no run event at all"
    event = captured[-1]

    assert event["operation"] == "aggregate_gold", (
        f"the gold promotion was recorded as job {event['operation']!r} -- lineage now says the silver stage produced acme-gold$catalog"
    )
    assert event["author"] == "analyst", f"the gold promotion was authored by {event['author']!r}, the bronze->silver mover's literal"
    assert event.get("version") == 48, (
        f"the promotion recorded version {event.get('version')!r}; the hold was taken on v48, and "
        "build_run_event's default of 1 claims a version the table never had at that point"
    )


def test_the_outputs_were_never_the_broken_half(captured: list[dict[str, Any]]) -> None:
    """Non-vacuity, and it pins the asymmetry that made this hard to see.

    Inputs and outputs come off the SPEC and were always right; only the fields read from `settings`
    were wrong. If a future refactor breaks these too, this test says so instead of the graph.
    """
    emit_promotion_outcome(None, _gold_hold())  # ty: ignore[invalid-argument-type]
    event = captured[-1]

    assert event["output_name"] == "acme-gold$catalog"
    assert event["inputs"] == [("silver", "acme-silver$features")]


def test_a_silver_hold_still_names_the_silver_stage(captured: list[dict[str, Any]]) -> None:
    """The accidental-pass case, made deliberate.

    A bronze->silver promotion produced the RIGHT answer before this fix, for the wrong reason -- the
    producer's defaults happen to be that stage's values. Pinning it stops a fix that merely swaps one
    hardcoded stage for another from looking correct.
    """
    report = _gold_hold()
    report.spec.from_namespace, report.spec.from_dataset = "bronze", "bronze$events"
    report.spec.to_namespace, report.spec.to_dataset = "silver", "silver$features"
    report.spec.operation, report.spec.author, report.spec.version = "embed_features", "data_eng", 72

    emit_promotion_outcome(None, report)  # ty: ignore[invalid-argument-type]
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
    report = _gold_hold()
    report.spec.operation, report.spec.author, report.spec.version = "", "", 0

    emit_promotion_outcome(None, report)  # ty: ignore[invalid-argument-type]
    event = captured[-1]

    assert event["operation"] == "embed_features", "an old spec must fall back to settings, not emit an empty job name"
    assert event["author"] == "data_eng"
    assert event["version"] == 1, "no recorded version falls back to build_run_event's own default"
