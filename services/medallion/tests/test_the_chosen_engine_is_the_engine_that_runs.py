"""The engine a stage CHOOSES is the engine that actually runs it, resolved through the port.

Q17-1..4, and G1b of the standing goal: "the two seams stay BYO, and the deployed path must use them".

WHAT IS ACTUALLY MISSING, measured 2026-09-07 rather than inferred from the import count.
`engine_choice.engine_for` answers with a STRING — `"ray"` or `"inprocess"` — and nothing in the
estate turns that string into an `Executor`. There is no registry. `InProcessExecutor` is constructed
by hand at `transform.py:760`, `RayJobExecutor` is constructed NOWHERE outside tests, and the lane the
estate actually runs reaches Ray through `ray_submit` — a second, older submission seam that the port
does not sit in front of. So the choice and the execution are two unconnected mechanisms that happen
to agree, and a port with two adapters of which one is dead is a decoupling claim rather than a
decoupled system.

THE NAME IS DEFINED THREE TIMES, which is the same defect one layer down. `RAY_ENGINE` is declared in
`task_register.py`, `rayjob_executor.py` AND `engine_choice.py`; `IN_PROCESS_ENGINE` in
`engine_choice.py` and `inprocess_executor.py`. They agree today. Nothing makes them agree tomorrow:
a rename in one is a stage that chooses an engine no adapter answers to, and the failure is an
`UnrunnableTaskError` naming an engine that looks correct in the file the reader happens to open.

WHY A REGISTRY RATHER THAN AN `if`. Two adapters and a conditional would work and would not be BYO:
adding a third engine would mean editing the conditional, which is the thing a port exists to stop.
A registry keyed on the declared name means a new engine is a new adapter and a new registration,
and `resolve_task_registration` already refuses a task registered for an engine this deployment does
not host — so the refusal path exists and only the resolution is missing.

This asserts the seam, not the transport: `Executor` is `runtime_checkable`, so conformance is ASKED.
What the adapter then does with Ray is the adapter's business and no test here reads it.
"""

from __future__ import annotations

import pytest

from service_kit.lakehouse.executor import Executor


def test_every_engine_name_has_exactly_one_definition() -> None:
    """A name declared in three modules is three chances to disagree, and they only have to disagree
    once. Imported from their several homes and compared, so a divergence reds here rather than at a
    stage that chose an engine nothing answers to."""
    from medallion.services import engine_choice, inprocess_executor, task_register

    ray_names = {
        "engine_choice": engine_choice.RAY_ENGINE,
        "task_register": task_register.RAY_ENGINE,
    }
    assert len(set(ray_names.values())) == 1, f"the ray engine is spelled differently per module: {ray_names}"

    inprocess_names = {
        "engine_choice": engine_choice.IN_PROCESS_ENGINE,
        "inprocess_executor": inprocess_executor.IN_PROCESS_ENGINE,
    }
    assert len(set(inprocess_names.values())) == 1, f"the in-process engine is spelled differently per module: {inprocess_names}"

    # ...and one of them must be the single source the others re-export, or "they agree" is a
    # coincidence this test is merely observing rather than a property the code holds.
    assert engine_choice.RAY_ENGINE is task_register.RAY_ENGINE, (
        "the ray engine name is re-declared rather than imported, so the two can drift to equal-but-not-identical strings"
    )


def test_the_inprocess_engine_resolves_to_an_adapter() -> None:
    """`engine_for` answers with a name; something must turn that name into the thing that runs it, or
    the choice is advisory and the execution is hard-coded elsewhere.

    Only `inprocess` is asserted here because only `inprocess` is executed through an `Executor`. Ray's
    path is `ray_submit` (the Ray Jobs API) — see the engine-coverage test below, which states the
    invariant over BOTH paths rather than pretending one shape covers them.
    """
    from medallion.services.engine_registry import executor_for

    executor = executor_for("inprocess", storage_options={})
    assert isinstance(executor, Executor), f"inprocess resolved to something that is not an Executor: {executor!r}"
    assert executor.name == "inprocess"


def test_an_unknown_engine_is_REFUSED_rather_than_defaulted() -> None:
    """Falling back to whichever engine is configured is how a declaration for another executor gets
    silently run here — the exact failure `engine_choice` already refuses at declaration time."""
    from medallion.services.engine_registry import UnknownEngineError, executor_for

    with pytest.raises(UnknownEngineError) as excinfo:
        executor_for("some-engine-nobody-hosts", storage_options={})
    assert "some-engine-nobody-hosts" in str(excinfo.value), "the refusal does not name the engine, so an operator cannot act on it"


def test_every_choosable_engine_has_a_path_that_runs_it() -> None:
    """The real invariant: a stage must never choose an engine nothing can execute.

    THIS USED TO ASSERT `hosted_engines() == KNOWN_ENGINES`, and that was true only while a Ray
    `Executor` adapter existed. It had ZERO production callers — the cascade has always submitted Ray
    work through `ray_submit` (the Ray Jobs API) — and it was deleted on 2026-09-15 (owner decision)
    rather than kept as a second live path nobody exercised.

    Equality would now be FALSE for an engine the estate runs every day, and a test asserting it would
    push the next person to re-add an adapter to satisfy the test rather than because anything calls
    it. So the invariant is stated over BOTH execution paths: the registry for `inprocess`, and
    `ray_submit` for `ray`. Widening `KNOWN_ENGINES` without adding a path still reds here, which is
    the property worth keeping.
    """
    from medallion.services import ray_submit
    from medallion.services.engine_choice import IN_PROCESS_ENGINE, KNOWN_ENGINES, RAY_ENGINE
    from medallion.services.engine_registry import hosted_engines

    runnable = set(hosted_engines())
    if callable(getattr(ray_submit, "submit_stage_job", None)):
        runnable.add(RAY_ENGINE)

    assert runnable >= set(KNOWN_ENGINES), (
        f"engine_choice may choose {sorted(KNOWN_ENGINES)} but only {sorted(runnable)} have a path that runs them — "
        "a stage can choose an engine nothing executes"
    )
    assert IN_PROCESS_ENGINE in hosted_engines(), "the registry must still resolve the in-process engine"
