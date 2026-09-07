"""Name -> adapter. The seam that turns a chosen engine into the thing that runs it.

`engine_choice.engine_for` answers with a STRING, and nothing turned that string into an `Executor`:
`InProcessExecutor` was constructed by hand at one call site, `RayJobExecutor` was constructed
nowhere outside tests, and the deployed lane reached Ray through a second, older submission seam the
port did not sit in front of. Choice and execution were two mechanisms that happened to agree.

A REGISTRY RATHER THAN A CONDITIONAL, because a conditional is the thing a port exists to stop: with
one, adding an engine means editing a branch every caller shares. Here a new engine is a new adapter
and a new row, and `resolve_task_registration` already refuses a task registered for an engine this
deployment does not host — the refusal path existed and only the resolution was missing.

CONSTRUCTION ARGUMENTS DIFFER PER ADAPTER AND THAT IS NOT A LEAK. `InProcessExecutor` needs the
credential resolver because it holds the Lance handle itself; `RayJobExecutor` needs the deployment
facts KubeRay's webhook requires (which cluster, which queue) and no credential at all, because the
Ray pods carry their own. So each adapter is built from what IT needs, and the caller passes both.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from medallion.services.engine_names import IN_PROCESS_ENGINE, RAY_ENGINE
from service_kit.lakehouse.executor import Executor


class UnknownEngineError(RuntimeError):
    """A declared engine no adapter answers to.

    RAISED, never defaulted: falling back to whichever engine happens to be configured is how a
    declaration meant for another executor gets silently run here, which is the failure
    `engine_choice` already refuses at declaration time. A default would reopen it at resolution time.
    """


def _as_callable(storage_options: Callable[[], dict[str, str]] | Mapping[str, str]) -> Callable[[], dict[str, str]]:
    """A resolver, from either a resolver or a fixed mapping.

    The in-process adapter takes a CALLABLE on purpose — credentials are resolved per run, and a value
    captured at construction would be as old as the process. A mapping is accepted for a caller that
    genuinely has no vending step (a test, a local drive) and is wrapped rather than special-cased.
    """
    if isinstance(storage_options, Mapping):
        # Narrowed on the MAPPING, not on `callable()`: a Mapping may itself be callable, so the
        # negative test leaves the union intact and the resolver's type becomes a guess.
        fixed = dict(storage_options)
        return lambda: fixed
    return storage_options


def executor_for(
    engine: str,
    *,
    storage_options: Callable[[], dict[str, str]] | Mapping[str, str],
    config: Mapping[str, Any] | None = None,
) -> Executor:
    """The adapter that runs `engine`, or `UnknownEngineError`.

    `config` carries the engine's DEPLOYMENT facts (for Ray: which cluster, which namespace, which
    Kueue queue). It is per-engine by nature and this module does not interpret it.
    """
    if engine == IN_PROCESS_ENGINE:
        from medallion.services.inprocess_executor import InProcessExecutor

        return InProcessExecutor(_as_callable(storage_options))
    if engine == RAY_ENGINE:
        from medallion.services.rayjob_executor import RayJobExecutor

        return RayJobExecutor(**dict(config or {}))
    raise UnknownEngineError(
        f"no executor is registered for engine {engine!r}; this deployment hosts {sorted(hosted_engines())}. "
        "A task registered for another engine belongs to another deployment and is refused rather than run here."
    )


def hosted_engines() -> frozenset[str]:
    """What this deployment can actually RESOLVE.

    Deliberately separate from `engine_choice.HOSTED_ENGINES`, which declares what a stage may
    CHOOSE. The two must agree, and a test asserts it: an engine that can be chosen and not resolved
    is a stage that dies at submission, and one that can be resolved and not chosen is a reachable
    path nobody reviewed.
    """
    return frozenset({IN_PROCESS_ENGINE, RAY_ENGINE})
