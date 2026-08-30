"""A Dapr activity receives a DICT, whatever its annotation says.

MEASURED on the live estate 2026-08-26, driving a real backfill — 600 objects from an S3 prefix
through `POST /api/ingest/ingests`::

    "status": "FAILED",
    "errors": {"run": "...: Activity task #7 failed: 'dict' object has no attribute 'run_id'"}
    "units_total": 0, "units_done": 0

Nothing was ingested. The run reported FAILED with a Python AttributeError as its user-facing reason.

THE MECHANISM, identical to the one already fixed in `services/medallion`. The orchestrator passes a
model (`input=RunSpec(...)`); Dapr serialises it to JSON to cross the durable boundary and hands the
activity the decoded dict. The type annotation is documentation — the SDK never reads it — so every
`payload.run_id` in an activity body is an AttributeError waiting for that activity to run.

The annotations were added deliberately (DWF-ACT-009) across medallion, flows AND ingest, on the
stated premise that "the SDK coerces an ACTIVITY's input into its annotated" type. That premise is
false. Medallion was fixed when its cascade died of it; ingest was not, and its own defect stayed
hidden until something actually ran a backfill. `services/flows` is clean and needs no change — its
activities take dicts.

WHY A GATE PER SERVICE. This is invisible in review by construction: the annotation makes
`payload.run_id` read correctly, type-check correctly, and pass any unit test that constructs the
model and calls the function directly — because such a test hands it the very object Dapr will not.
The medallion copy of this gate exists for the same reason; this is its sibling, not a duplicate,
because each service registers its own activities and neither imports the other.
"""

from __future__ import annotations

import inspect
import re

from pydantic import BaseModel

from ingest import workflow


def _activities() -> list[tuple[str, str, type]]:
    """(function name, parameter name, model type) for every registered activity taking a model."""
    found: list[tuple[str, str, type]] = []
    for fn in workflow.ACTIVITIES:
        params = list(inspect.signature(fn).parameters.items())
        if len(params) < 2:
            continue
        name, param = params[1]
        annotation = param.annotation
        if isinstance(annotation, str):
            annotation = getattr(workflow, annotation, None)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.append((fn.__name__, name, annotation))
    return found


def test_the_scan_finds_the_registered_activities() -> None:
    """Non-vacuity: an empty roster would make the assertion below pass while checking nothing."""
    activities = _activities()
    assert len(activities) >= 4, f"only {len(activities)} model-taking activities found; the registry moved"


def test_every_activity_coerces_its_payload_before_using_it() -> None:
    """An activity that dereferences its payload must validate it first — the SDK will not."""
    offenders: list[str] = []
    for fn_name, param, model in _activities():
        body = inspect.getsource(getattr(workflow, fn_name))
        dereferences = re.search(rf"\b{re.escape(param)}\.\w+", body)
        coerces = f"{model.__name__}.model_validate" in body
        if dereferences and not coerces:
            offenders.append(f"{fn_name}({param}: {model.__name__})")

    assert not offenders, (
        "these activities read attributes off a payload Dapr delivers as a dict, so each raises "
        "AttributeError the moment a real run calls it:\n  " + "\n  ".join(offenders)
    )
