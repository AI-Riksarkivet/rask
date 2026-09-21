"""`workflow.py` asks the ENGINE PORT about a run, never `ray_jobs_api` directly.

[[LH-159]] DONE-CRITERION 3 VERBATIM: the lakehouse "is NOT coupled to a workflow engine or to Ray".
Measured 2026-09-21, `workflow.py` imported `ray_jobs_api` and `ray_submit` at four call sites, so the
cascade's workflow reached Ray by import — coupling of exactly the kind that criterion forbids, while
`engine_registry.executor_for` sat beside it already implemented and already used by `transform.py`.

THIS GATE COVERS EVERY PATH — status, failure and SUBMIT. The submit path needed the order
construction lifted out of `ray_submit` first; `submit_stage_job` now lives in the engine-agnostic
`stage_submit` and posts through `executor_for(RAY_ENGINE)`, so no path reaches Ray by import from
here and the gate says so for all three rather than for two.

WHY AN IMPORT GATE AND NOT A BEHAVIOUR TEST. Behaviour tests pass equally well against a direct call
and against the port, because the port DELEGATES to the same functions — `RayJobsApiExecutor` wraps
`job_status` and `job_failure` rather than reimplementing them, deliberately. What is being enforced
here is the direction of the dependency, and only the import graph shows that.
"""

from __future__ import annotations

from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "src/medallion/workflow.py"


def test_the_source_is_readable_and_substantial() -> None:
    """Without this a moved or emptied file would pass every assertion below."""
    text = SOURCE.read_text()

    assert len(text) > 10_000, f"{SOURCE} is {len(text)} bytes; the gate is reading the wrong file"


def test_the_workflow_does_not_import_the_ray_jobs_api() -> None:
    """RED before the fix: three call sites imported `job_status` / `job_failure` directly."""
    offenders = [f"{n}: {line.strip()}" for n, line in enumerate(SOURCE.read_text().splitlines(), 1) if "import" in line and "ray_jobs_api" in line]

    assert not offenders, (
        "the cascade's workflow reaches Ray's Jobs API by import, which is the coupling done-criterion 3 "
        "forbids; ask `executor_for(RAY_ENGINE)` instead:\n  " + "\n  ".join(offenders)
    )


def test_the_workflow_does_not_open_a_ray_client() -> None:
    """`ray_client` is the same coupling wearing a transport's name.

    The executor owns its pooled client — that is what `RayJobsApiExecutor.__init__` documents — so a
    workflow reaching for one is reaching past the port to the engine's transport.
    """
    offenders = [f"{n}: {line.strip()}" for n, line in enumerate(SOURCE.read_text().splitlines(), 1) if "import" in line and "ray_client" in line]

    assert not offenders, "the workflow opens Ray's HTTP client itself; the executor owns the pooled client:\n  " + "\n  ".join(offenders)


def test_the_workflow_resolves_its_engine_by_name() -> None:
    """The positive half: it must reach the engine THROUGH the registry, not by naming a class.

    `engine_registry.executor_for` is what turns a chosen engine into the thing that runs it. Naming
    `RayJobsApiExecutor` here would swap one hard-coded engine for another and satisfy the two
    assertions above while changing nothing that matters.
    """
    lines = SOURCE.read_text().splitlines()

    assert any("executor_for" in line for line in lines), "the workflow never resolves an executor, so it is not going through the port at all"
    # AN IMPORT, NOT A MENTION. The helper's own docstring names the adapter in order to say why it is
    # not used, and a bare substring test reads that prose as the defect it forbids — the same mistake
    # `test_no_numeric_helm_default_can_swallow_an_explicit_zero` made with `| default`.
    named = [f"{n}: {line.strip()}" for n, line in enumerate(lines, 1) if "import" in line and "RayJobsApiExecutor" in line]
    assert not named, "the workflow imports the Ray adapter directly; resolve it by engine name instead:\n  " + "\n  ".join(named)


def test_the_workflow_does_not_import_the_RAY_NAMED_submitter() -> None:
    """The submit path, held to the same bar as the two read paths above.

    `ray_submit` is the train lane's module: it hand-rolls a Ray Jobs body and owns the pooled Ray
    client. Importing it here put a module named for one engine on the cascade's only submit path,
    which is the dependency direction done-criterion 3 forbids however well the function behind it
    behaves. The stage submitter lives in `stage_submit`, which names no engine but the one the
    dispatch already chose.

    THE FUNCTION'S NAME IS NOT WHAT IS CHECKED, deliberately — a gate on `submit_stage_job` would pass
    the moment someone imported something else out of the same module, and the module is the coupling.
    """
    offenders = [f"{n}: {line.strip()}" for n, line in enumerate(SOURCE.read_text().splitlines(), 1) if "import" in line and "ray_submit" in line]

    assert not offenders, (
        "the cascade's workflow imports the Ray-named submitter; the engine-agnostic `stage_submit` is the one to reach for:\n  " + "\n  ".join(offenders)
    )
