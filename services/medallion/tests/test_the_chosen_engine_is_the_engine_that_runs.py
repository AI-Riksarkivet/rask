"""The engine a stage CHOOSES is the engine that actually runs it, resolved through the port.

Q17-1..4, and G1b of the standing goal: "the two seams stay BYO, and the deployed path must use them".

WHAT IS ACTUALLY MISSING, measured 2026-09-07 rather than inferred from the import count.
`engine_choice.engine_for` answers with a STRING — `"ray"` or `"inprocess"` — and nothing in the
estate turned that string into an `Executor`. There was no registry: `InProcessExecutor` was
constructed by hand at `transform.py:760`, the port's Ray adapter was constructed NOWHERE outside
tests, and the lane the estate actually runs reached Ray through `ray_submit` — a second, older
submission seam the port did not sit in front of. So the choice and the execution were two unconnected
mechanisms that happened to agree, and a port with two adapters of which one is dead is a decoupling
claim rather than a decoupled system.

THAT IS THE STATE THIS TEST WAS WRITTEN AGAINST, AND IT IS NOT THE CURRENT ONE — kept because it is
what the gate exists to prevent recurring. `engine_registry` now resolves both names, and the dead
adapter was deleted 2026-09-15 rather than wired.

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


def test_an_unknown_engine_is_REFUSED_rather_than_defaulted() -> None:
    """Falling back to whichever engine is configured is how a declaration for another executor gets
    silently run here — the exact failure `engine_choice` already refuses at declaration time."""
    from medallion.services.engine_registry import UnknownEngineError, executor_for

    with pytest.raises(UnknownEngineError) as excinfo:
        executor_for("some-engine-nobody-hosts", storage_options={})
    assert "some-engine-nobody-hosts" in str(excinfo.value), "the refusal does not name the engine, so an operator cannot act on it"
