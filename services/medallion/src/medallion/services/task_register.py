"""Register what this estate's planes can run — the WRITE half of the task registry.

docs/DECISIONS.md "The compute plane is decoupled" step 1. A transform declares a TASK; the registry says what
running it means. The two are separated so the catalog's declaration door can refuse an unrunnable
transform without learning any engine's vocabulary — which is only true if the registry is written
by a plane that HAS one.

BOTH OF THIS ESTATE'S PLANES REGISTER HERE, each stamping its own engine: the producer submits to
Ray, and it also hosts the in-process engine. A third executor registers its own tasks under the same
prefix, and the catalog changes not at all.

The in-process half is not an afterthought — without it a Ray-less estate wrote NO registrations, so
`_tasks/` was empty, the catalog's declaration door answered 422 for every transform, and the only lane
that still ran was the UNDECLARED chart-flag fallback. An estate that brought its own engine could run
the cascade only by declaring nothing, which is the opposite of what BYO means.

Written at boot rather than by a bootstrap Job, because the writer must be a thing that can also
RUN the task — a Job that registers and exits could keep asserting a capability after the plane that
provides it was scaled to zero. The write is idempotent (``put_task`` overwrites), so a restart
re-asserts rather than duplicating.

Blocking IO; the caller threadpools it.
"""

from __future__ import annotations

import logging

from medallion.core.config import MedallionSettings
from medallion.services import engine_names
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.task_registry import TaskRegistration


log = logging.getLogger(__name__)

#: The engine names these planes answer to. Kept beside the writer rather than in `service-kit`,
#: because the platform must never hold an engine vocabulary — that is the whole decoupling.
RAY_ENGINE = engine_names.RAY_ENGINE
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
    declared = [(task, RAY_ENGINE) for task in settings.ray_tasks] + [(task, IN_PROCESS_ENGINE) for task in settings.inprocess_tasks]
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
            code_version=task.code_version or settings.ray_code_version,
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
    log.info("ray_tasks_registered", extra={"declared": len(settings.ray_tasks), "registered": landed, "control_root": settings.control_root})
    return landed
