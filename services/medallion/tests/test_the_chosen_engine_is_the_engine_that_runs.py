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
    from medallion.services import engine_choice, inprocess_executor, rayjob_executor, task_register

    ray_names = {
        "engine_choice": engine_choice.RAY_ENGINE,
        "rayjob_executor": rayjob_executor.RAY_ENGINE,
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
    assert engine_choice.RAY_ENGINE is rayjob_executor.RAY_ENGINE, (
        "the ray engine name is two separate string objects, i.e. two definitions that happen to match — one module must own it and the others import it"
    )


@pytest.mark.parametrize("engine", ["ray", "inprocess"])
def test_a_chosen_engine_resolves_to_an_adapter(engine: str) -> None:
    """THE MISSING SEAM. `engine_for` answers with a name; something must turn that name into the
    thing that runs it, or the choice is advisory and the execution is hard-coded elsewhere."""
    from medallion.services.engine_registry import executor_for

    executor = executor_for(engine, storage_options={})
    assert isinstance(executor, Executor), f"{engine!r} resolved to something that is not an Executor: {executor!r}"
    assert executor.name == engine, f"{engine!r} resolved to an adapter that calls itself {executor.name!r}"


def test_an_unknown_engine_is_REFUSED_rather_than_defaulted() -> None:
    """Falling back to whichever engine is configured is how a declaration for another executor gets
    silently run here — the exact failure `engine_choice` already refuses at declaration time."""
    from medallion.services.engine_registry import UnknownEngineError, executor_for

    with pytest.raises(UnknownEngineError) as excinfo:
        executor_for("some-engine-nobody-hosts", storage_options={})
    assert "some-engine-nobody-hosts" in str(excinfo.value), "the refusal does not name the engine, so an operator cannot act on it"


def test_the_registry_hosts_exactly_what_engine_choice_claims() -> None:
    """`HOSTED_ENGINES` is asserted by the suite so widening it is a reviewed change. A registry that
    resolves a different set makes that review meaningless in one direction or the other."""
    from medallion.services.engine_choice import HOSTED_ENGINES
    from medallion.services.engine_registry import hosted_engines

    assert hosted_engines() == HOSTED_ENGINES, (
        f"the registry hosts {sorted(hosted_engines())} while engine_choice claims {sorted(HOSTED_ENGINES)} — "
        "a stage can choose an engine nothing resolves, or an engine nobody may choose is reachable"
    )
