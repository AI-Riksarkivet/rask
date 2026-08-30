"""A Dapr activity receives a DICT, whatever its annotation says.

MEASURED on the live estate 2026-08-26, driving a real cascade with `POST /produce`::

    stage-ray-silver-proof2-1787733931-2858ee2c7561
    Activity execution failed - task_id: 16, error: 'dict' object has no attribute 'outcome'

The orchestrator passes a model (`input=StageReport(spec=spec, outcome=outcome)`); Dapr serialises
it to JSON to cross the durable boundary, and the activity is handed the decoded dict. The type
annotation is documentation, not a coercion — the SDK never reads it. So every `payload.outcome` in
an activity body is an AttributeError waiting for that activity to run.

WHY THIS IS WORTH A GATE RATHER THAN TEN EDITS. The annotations were added deliberately (DWF-ACT-009)
on the stated premise that "the SDK coerces an ACTIVITY's input into its annotated" type. That premise
is false, and it is invisible in review precisely because the annotation makes the body LOOK correct —
`payload.outcome` reads as fine, type-checks as fine, and fails only when a real workflow runs it.
Unit tests that construct the model and call the function directly pass too, because they hand it the
very object Dapr will not.

The cost is total: the stage reporter is on the failure path of the medallion cascade, so a broken
cascade could not even record that it was broken.
"""

from __future__ import annotations

import inspect
import re

from pydantic import BaseModel

from medallion import workflow


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
    assert len(activities) >= 6, f"only {len(activities)} model-taking activities found; the registry moved"
    assert {n for n, _, _ in activities} >= {"report_stage_outcome", "publish_stage_ready"}, "the cascade's own reporters are not in the scan"


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
        "AttributeError the moment a real workflow calls it:\n  " + "\n  ".join(offenders) + "\n"
        f"Add `{'{param}'} = {'{Model}'}.model_validate({'{param}'})` at the top of each body."
    )
