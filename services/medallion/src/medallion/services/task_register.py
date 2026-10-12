"""Register what this estate's planes can run — the WRITE half of the task registry.

docs/adr/0052-the-compute-plane-is-decoupled-a-port-two-adapters-and-no.md "The compute plane is decoupled" step 1. A transform declares a TASK; the registry says what
running it means. The two are separated so the catalog's declaration door can refuse an unrunnable
transform without learning any engine's vocabulary — which is only true if the registry is written
by a plane that HAS one.

EVERY ENGINE THIS DEPLOYMENT CONFIGURES IS REGISTERED HERE, each task stamped with the engine whose list holds it:
the in-process engine's own list, and each `MEDALLION_ENGINES` entry's (CP-056). Another engine is another entry, and
the catalog changes not at all.

The in-process half is not an afterthought — without it a Ray-less estate wrote NO registrations, so
`_tasks/` was empty, the catalog's declaration door answered 422 for every transform, and the only lane
that still ran was the UNDECLARED chart-flag fallback. An estate that brought its own engine could run
the cascade only by declaring nothing, which is the opposite of what BYO means.

Written at the producer's boot from the deployment's configuration: the registrar is the deployment, which names
the engines it runs. An engine scaled to zero leaves its runs unanswered, and the plan sweep resolves them. The write
is idempotent (``put_task`` overwrites), so a restart re-asserts rather than duplicating.

Blocking IO; the caller threadpools it.
"""

from __future__ import annotations

import logging

from medallion.core.config import MedallionSettings
from medallion.services import engine_names
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.task_registry import TaskRegistration


log = logging.getLogger(__name__)

IN_PROCESS_ENGINE = engine_names.IN_PROCESS_ENGINE


def register_tasks(settings: MedallionSettings) -> int:
    """Write every chart-declared task into ``<control_root>/_tasks/``; return how many landed.

    Each plane's list is stamped with THAT plane's engine, so an estate hosting both registers both
    vocabularies and the catalog resolves each declared task to the plane that actually hosts it.

    NON-FATAL, and the failure is legible rather than silent: a registration that cannot be written
    leaves the catalog's declaration door answering 422 naming the exact task that is missing, which
    is a better answer than a cascade head that refuses to start. Crashing here would take down
    ``/produce`` — the whole ingest surface — over a capability nothing has asked for yet.
    """
    declared = [(task, IN_PROCESS_ENGINE) for task in settings.inprocess_tasks]
    declared += [(task, name) for name, engine in settings.engines.items() for task in engine.tasks]
    if not declared:
        return 0
    if not settings.control_root:
        log.error(
            "tasks_unregisterable",
            extra={"tasks": len(declared), "reason": "MEDALLION_CONTROL_ROOT is empty; transform declarations naming these tasks will be refused"},
        )
        return 0
    storage_options = settings.storage_options()
    landed = 0
    for task, engine in declared:
        registration = TaskRegistration(
            task=task.task,
            engine=engine,
            command=task.command,
            code_version=task.code_version or settings.engine_code_version(engine),
            cardinalities=task.cardinalities,
            obligations=task.obligations,
        )
        try:
            task_registry.put_task(settings.control_root, storage_options, registration)
        except Exception:
            # One unwritable record must not cost the others theirs — the tasks are independent, and
            # a partial registry refuses exactly the declarations it cannot honour.
            log.exception("task_registration_failed", extra={"task": task.task, "engine": engine})
            continue
        landed += 1
    # Counted over every engine's declarations, so the two numbers answer the same question.
    log.info(
        "tasks_registered",
        extra={"declared": len(declared), "registered": landed, "engines": sorted({engine for _, engine in declared}), "control_root": settings.control_root},
    )
    return landed
