"""WHICH compute engine runs a stage — answered by the RECORD, then by the deployment's default.

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled" **Two axes, and this module is only the second one.**

* **ORCHESTRATION** — when a stage runs, what happens next, what happens if it dies — is Dapr
  pub/sub's and the planned lane's (`stage_plans`, CP-029). Nothing here touches it: the trigger, the
  plan, its sweep and the re-publish are the same whichever engine below is chosen.
* **COMPUTE** — what machine moves the bytes — is what a task's registration names, and what this
  module reads.

A DECLARATION decides: it names the engine whose registration of its task runs it, or, naming none, the task's one
hosted registration, or the default engine among several. A stage runner that declares nothing runs on
``MEDALLION_DEFAULT_ENGINE``. An engine is a name: `inprocess`, or a key the deployment configured in
``MEDALLION_ENGINES`` (CP-056), so this module knows no engine but its own.

**An engine this deployment does not host is REFUSED, never defaulted.** A declaration meant for
another plane must not quietly run here on whatever is available: that is how the wrong program
rewrites a tenant's data while every status says success.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi.concurrency import run_in_threadpool

from medallion.services.engine_registry import hosted_engines
from medallion.services.transform_spec import UnrunnableTaskError, _TransformSettings
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.task_registry import TaskRegistration


if TYPE_CHECKING:
    from medallion.core.config import MedallionSettings
    from service_kit.lakehouse.transform_specs import TransformSpec


log = logging.getLogger(__name__)


def engine_for(settings: MedallionSettings, *, spec: TransformSpec | None) -> str:
    """The engine this stage runs on.

    ``spec`` is the resolved declaration, or ``None`` when this stage runner declares no transform.

    Raises :class:`UnrunnableTaskError` when the declaration names a task nobody registered, or one
    registered for an engine this deployment does not host. Both are operator errors that no
    redelivery can fix, and both share the type the cascade already drops-with-a-trace on.
    """
    if spec is None:
        log.debug("stage_engine_from_chart", extra={"engine": settings.default_engine})
        return settings.default_engine
    registrations = registrations_of(settings, task=spec.task)
    hosted = hosted_engines(settings)
    if spec.engine:
        chosen = [r.engine for r in registrations if r.engine == spec.engine]
        if not chosen:
            raise UnrunnableTaskError(f"task {spec.task!r} is not registered for engine {spec.engine!r}, which the declaration names")
    else:
        runnable = [r.engine for r in registrations if r.engine in hosted]
        chosen = [settings.default_engine] if settings.default_engine in runnable else runnable
        if len(chosen) > 1:
            raise UnrunnableTaskError(
                f"task {spec.task!r} is registered for engines {chosen}, the default engine {settings.default_engine!r} is not one "
                "of them, and the declaration names none; declare its engine"
            )
        if not chosen:
            chosen = [r.engine for r in registrations]
    engine = chosen[0]
    if engine not in hosted:
        raise UnrunnableTaskError(
            f"task {spec.task!r} is registered for engine {engine!r}, which this deployment does not host "
            f"(it hosts {sorted(hosted)}; an engine is hosted when MEDALLION_ENGINES names it). Refusing rather than "
            "running it on whichever engine happens to be configured here."
        )
    log.info("stage_engine_from_declaration", extra={"transform": spec.name, "task": spec.task, "engine": engine})
    return engine


def registrations_of(settings: _TransformSettings, *, task: str) -> list[TaskRegistration]:
    """Every engine's registration of a declared task, refusing a task no engine registered."""
    control_root = settings.control_root
    if not control_root:
        raise UnrunnableTaskError(
            f"task {task!r} cannot be resolved: MEDALLION_CONTROL_ROOT is empty, so the registry cannot be read. "
            "Set it to the same control root the catalog writes _tasks/ under."
        )
    registrations = task_registry.registrations_of(control_root, settings.storage_options(), task)
    if not registrations:
        raise UnrunnableTaskError(
            f"no task is registered as {task!r}; a task is registered by the plane that can run it, under {control_root}/_tasks/. "
            "A declaration can outlive the registration it was checked against, so this is refused here as well as at the door."
        )
    return registrations


async def engine_for_async(settings: MedallionSettings, *, spec: TransformSpec | None) -> str:
    """:func:`engine_for` off the event loop: the registry read is a blocking object-store call."""
    if spec is None:
        return engine_for(settings, spec=None)
    return await run_in_threadpool(engine_for, settings, spec=spec)
