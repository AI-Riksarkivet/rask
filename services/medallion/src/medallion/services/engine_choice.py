"""WHICH compute engine runs a stage — answered by the RECORD, not by a deployment flag.

docs/DECISIONS.md "The compute plane is decoupled" **Two axes, and this module is only the second one.**

* **ORCHESTRATION** — when a stage runs, what happens next, what happens if it dies — is Dapr
  Workflow's and Dapr pub/sub's, estate-wide and by recorded decision (`.claude/skills/rask-dapr`).
  Nothing here touches it: the trigger, the durable timer and the re-publish are the same whichever
  engine below is chosen.
* **COMPUTE** — what machine moves the bytes — is what a task's registration names, and what this
  module reads.

Keeping them apart is what makes each replaceable on its own. A workflow engine that also decided
where the bytes run, or an engine choice that also implied a retry policy, would be one thing wearing
two jobs.

**The opt-in default is load-bearing and unchanged.** An estate that declares no transform is
governed by ``MEDALLION_RAY_ENABLED`` exactly as it always was. What a DECLARATION does is take the
decision over — because the alternative is a record that says ``engine: "ray"`` beside code that
decided from a boolean, which is half a decoupling and reads exactly like a whole one.

**An engine this deployment does not host is REFUSED, never defaulted.** A declaration meant for
another plane must not quietly run here on whatever is available: that is how the wrong program
rewrites a tenant's data while every status says success.
"""

from __future__ import annotations

import logging
from typing import Final, Protocol

from fastapi.concurrency import run_in_threadpool

from medallion.services import engine_names
from medallion.services.transform_spec import UnrunnableTaskError, _TransformSettings
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.task_registry import TaskRegistration
from service_kit.lakehouse.transform_specs import TransformSpec


log = logging.getLogger(__name__)

#: The engine that submits to the Ray cluster (`ray_submit`).
RAY_ENGINE: Final = engine_names.RAY_ENGINE
#: The engine that reads, transforms and writes inside this process (`compute.transform_stage`).
#: A real second engine, not a fallback: it is what an estate without a Ray cluster runs on, and it
#: is the cheapest proof the contract is engine-plural.
IN_PROCESS_ENGINE: Final = engine_names.IN_PROCESS_ENGINE

#: Engines this BUILD carries an adapter for. A third engine is added by hosting it and registering
#: tasks for it — never by editing a branch, which is the shape that made the estate single-engine in
#: the first place. It is the CEILING on what :func:`hosted_engines` may answer, never the answer
#: itself: what a build can resolve and what a deployment runs are different questions.
KNOWN_ENGINES: Final = frozenset({RAY_ENGINE, IN_PROCESS_ENGINE})


class _EngineSettings(_TransformSettings, Protocol):
    """What choosing needs: the registry's location, plus the chart's own answer for an estate that
    has declared nothing. A Protocol so a test needs no full `MedallionSettings`."""

    ray_enabled: bool


def hosted_engines(settings: _EngineSettings) -> frozenset[str]:
    """The engines THIS DEPLOYMENT can actually run.

    A DEPLOYMENT FACT, NOT A CONSTANT, and that is the whole of it. Declared as a frozenset containing
    Ray unconditionally, the refusal below could never fire for Ray: a task registered for `ray` on a
    deployment with the Ray lane off passed the check, `_dispatch_stage_workflow` enqueued it through a
    client that only ENQUEUES, and `stage_runner` starts the runtime that would execute it only
    `if settings.ray_enabled`. Scheduled, never executed, and silent — no failure, no DLQ, no refusal.

    ``ray_enabled`` is read here for the SECOND of its two jobs. It is also the chart's default engine
    for an estate that has declared nothing (see :func:`engine_for`), and only this one — does this pod
    start the Ray workflow runtime — may gate a declaration. A declaration still overrides the chart in
    the direction that carries the decoupling: a task registered for in-process is honoured on a Ray-ON
    deployment, because in-process needs no runtime.

    Narrower than :data:`KNOWN_ENGINES` by construction, and `engine_registry.hosted_engines` is the
    same ceiling from the adapter side — a stage must never choose an engine nothing can resolve.
    """
    return KNOWN_ENGINES if settings.ray_enabled else frozenset({IN_PROCESS_ENGINE})


def engine_for(settings: _EngineSettings, *, spec: TransformSpec | None) -> str:
    """The engine this stage runs on.

    ``spec`` is the resolved declaration, or ``None`` when this stage runner declares no transform — the
    caller has already resolved it (and already refused an undeclared one), so this asks no second
    question of the object store beyond the task's own registration.

    Raises :class:`UnrunnableTaskError` when the declaration names a task nobody registered, or one
    registered for an engine this deployment does not host. Both are operator errors that no
    redelivery can fix, and both share the type the cascade already drops-with-a-trace on.
    """
    if spec is None:
        chosen = RAY_ENGINE if settings.ray_enabled else IN_PROCESS_ENGINE
        log.debug("stage_engine_from_chart", extra={"engine": chosen})
        return chosen
    registration = resolve_task_registration(settings, task=spec.task)
    hosted = hosted_engines(settings)
    if registration.engine not in hosted:
        # TWO REFUSALS WEARING ONE TYPE, because both are operator errors no redelivery fixes — but the
        # fixes differ, so the message names which. An engine this BUILD has no adapter for is a
        # declaration for another plane; one the build knows and this deployment has turned OFF is a
        # lever, and saying only "it hosts [inprocess]" leaves the operator to guess which.
        lever = (
            f" This build can run {registration.engine!r}, but this deployment does not: the Ray lane's workflow runtime "
            "starts only when MEDALLION_RAY_ENABLED is true, so the stage would be enqueued and never executed."
            if registration.engine in KNOWN_ENGINES
            else " The declaration is valid and belongs to another executor."
        )
        raise UnrunnableTaskError(
            f"task {spec.task!r} is registered for engine {registration.engine!r}, which this deployment does not host "
            f"(it hosts {sorted(hosted)}).{lever} Refusing rather than running it on whichever engine happens to be "
            "configured here."
        )
    log.info("stage_engine_from_declaration", extra={"transform": spec.name, "task": spec.task, "engine": registration.engine})
    return registration.engine


def resolve_task_registration(settings: _TransformSettings, *, task: str) -> TaskRegistration:
    """The registration for a declared task, whatever engine it names.

    Separate from :func:`transform_spec.resolve_task`, which asks the narrower question a SUBMITTER
    asks — "is this mine?" — and refuses anything else. Choosing needs the answer before it can
    decide, so it must not be refused for not being Ray.
    """
    control_root = settings.control_root
    if not control_root:
        raise UnrunnableTaskError(
            f"task {task!r} cannot be resolved: MEDALLION_CONTROL_ROOT is empty, so the registry cannot be read. "
            "Set it to the same control root the catalog writes _tasks/ under."
        )
    registration = task_registry.get_task(control_root, settings.storage_options(), task)
    if registration is None:
        raise UnrunnableTaskError(
            f"no task is registered as {task!r}; a task is registered by the plane that can run it, under {control_root}/_tasks/. "
            "A declaration can outlive the registration it was checked against, so this is refused here as well as at the door."
        )
    return registration


async def engine_for_async(settings: _EngineSettings, *, spec: TransformSpec | None) -> str:
    """:func:`engine_for` off the event loop.

    The registry read is a blocking object-store call and this runs inside a stage handler, so the
    synchronous form would stall the loop for every other delivery on the pod. Skips the threadpool
    hop entirely when there is no declaration, because then there is no IO to do.
    """
    if spec is None:
        return engine_for(settings, spec=None)
    return await run_in_threadpool(engine_for, settings, spec=spec)
