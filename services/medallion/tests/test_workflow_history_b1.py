"""B1: workflow history carries POINTERS, never payloads.

A Dapr Workflow persists its input to the state store on every checkpoint, and `continue_as_new`
re-persists the carried spec once per poll turn — up to `MAX_POLLS` times. So a field that holds
data rather than a reference to data is not merely wasteful: it is the same bytes written to
Postgres thousands of times, and it is the difference between a state store that holds coordination
state and one that has quietly become a second copy of the lakehouse.

`StageJobSpec` is payload-free today. Nothing PINNED that, which is the gap these tests close — the
model is the kind of thing someone extends under deadline ("just carry the rows so the activity
doesn't have to re-read them"), and the cost would not show up until the state store did.

The assertions are deliberately about SHAPE and SIZE rather than about a field blocklist. A blocklist
only catches the names someone thought of; a size ceiling catches the class, including the field
nobody has invented yet.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from medallion.workflow import MAX_POLLS, POLL_INTERVAL_SECONDS, StageJobOutcome, StageJobSpec


#: A realistic spec — the shape the mover actually dispatches, including a full trigger envelope and
#: a consume-layer provenance document.
def _spec(**over: Any) -> StageJobSpec:
    base: dict[str, Any] = {
        "from_uri": "s3://lance-catalog/medallion/bronze",
        "to_uri": "s3://lance-catalog/medallion/silver",
        "stage": "silver",
        "token": "tok-2026-08-17-abc123",
        "lineage_json": json.dumps({"run_id": "r-1", "inputs": [{"namespace": "bronze", "name": "bronze$events", "version": 7}], "operation": "transform"}),
        "trigger": {
            "stage": "bronze",
            "token": "tok-2026-08-17-abc123",
            "dataset": "bronze$events",
            "uri": "s3://lance-catalog/medallion/bronze",
            "version": 7,
            "event_time": "2026-08-17T21:00:00+00:00",
            "project": "acme",
        },
    }
    return StageJobSpec.model_validate(base | over)


#: The ceiling. Generous enough that a genuine pointer set (URIs, ids, a provenance doc, a trigger)
#: never trips it, tight enough that ONE batch of rows does. A 64 KiB spec re-persisted every 30 s
#: for 24 h is ~180 MB of state-store writes for a single stage run.
_SPEC_CEILING_BYTES = 8 * 1024


def test_a_realistic_spec_is_POINTER_SIZED() -> None:
    """The measurement that makes the rule enforceable rather than aspirational."""
    encoded = _spec().model_dump_json().encode()

    assert len(encoded) < _SPEC_CEILING_BYTES, (
        f"the workflow spec is {len(encoded)} B, over the {_SPEC_CEILING_BYTES} B pointer ceiling.\n"
        "A workflow input is persisted on EVERY checkpoint and re-persisted once per continue_as_new "
        "turn, so this is written to the state store up to MAX_POLLS times per run. Carry a URI and a "
        "version; let the activity read the data."
    )


def test_NO_field_can_hold_raw_bytes() -> None:
    """A `bytes` field is the unambiguous form of the defect, so it is refused by type.

    Pydantic would happily base64 it into the JSON checkpoint, where it is invisible in a code review
    of the workflow body — the body never mentions the field, it just carries it.
    """
    for model in (StageJobSpec, StageJobOutcome):
        for name, field in model.model_fields.items():
            assert field.annotation is not bytes, f"{model.__name__}.{name} is `bytes` — workflow history must carry a reference, not the data"


def test_the_TRIGGER_dict_is_carried_but_not_trusted_to_be_small() -> None:
    """`trigger` is the one open-ended field, held as a dict so the contract can grow.

    That openness is deliberate (a field added to the trigger must not silently drop off the round
    trip) and it is exactly where payload would arrive. The ceiling above is what bounds it, so this
    test states the risk explicitly and proves the ceiling actually catches it.
    """
    fat = _spec(trigger={"rows": ["x" * 1024 for _ in range(64)]})

    assert len(fat.model_dump_json().encode()) > _SPEC_CEILING_BYTES, (
        "a payload-carrying trigger slipped under the ceiling — the ceiling is too loose to be a gate"
    )


def test_the_outcome_carries_only_a_VERDICT_and_counters() -> None:
    """The return value is persisted too, and it is the other end of the same rule."""
    outcome = StageJobOutcome(submission_id="ray-silver-abc", status="SUCCEEDED", polls=3, verdict="succeeded")

    encoded = outcome.model_dump_json().encode()
    assert len(encoded) < 1024, f"the outcome is {len(encoded)} B — it should be a verdict and counters"


def test_the_carried_fields_are_what_makes_continue_as_new_SAFE() -> None:
    """Not a size assertion — the correctness one that sits beside it.

    `submission_id` and `polls_done` are carried precisely because each turn starts with EMPTY
    history. Drop them and the next turn re-submits the same stage job, once per poll interval,
    forever, each run overwriting the same output dataset — and the ceiling never arrives because the
    count restarts at zero. This pins the two fields against a "spec cleanup" that removes them for
    looking redundant.
    """
    carried = _spec(submission_id="ray-silver-abc", polls_done=5)
    round_tripped = StageJobSpec.model_validate(json.loads(carried.model_dump_json()))

    assert round_tripped.submission_id == "ray-silver-abc", "a turn that forgets its submission id re-submits the job"
    assert round_tripped.polls_done == 5, "a turn that forgets its poll count can never reach the ceiling"


@pytest.mark.parametrize(
    ("field", "value"),
    [("poll_interval_seconds", POLL_INTERVAL_SECONDS), ("max_polls", MAX_POLLS)],
)
def test_the_history_bound_has_concrete_defaults(field: str, value: int) -> None:
    """The bound is what keeps history finite at all; a `None` default would mean 'unbounded'."""
    assert getattr(_spec(), field) == value
    assert isinstance(value, int) and value > 0
