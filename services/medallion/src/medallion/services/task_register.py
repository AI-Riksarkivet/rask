"""Register what this estate's Ray plane can run — the WRITE half of the task registry.

`open_compute-decoupling.md` §7.4 step 1. A transform declares a TASK; the registry says what
running it means. The two are separated so the catalog's declaration door can refuse an unrunnable
transform without learning any engine's vocabulary — which is only true if the registry is written
by a plane that HAS one.

That plane is this one: the medallion producer submits to Ray, so it stamps ``engine="ray"`` on
every registration it writes. A second executor registers its own tasks under the same prefix, and
the catalog changes not at all.

Written at boot rather than by a bootstrap Job, because the writer must be a thing that can also
RUN the task — a Job that registers and exits could keep asserting a capability after the plane that
provides it was scaled to zero. The write is idempotent (``put_task`` overwrites), so a restart
re-asserts rather than duplicating.

Blocking IO; the caller threadpools it.
"""

from __future__ import annotations

import logging

from medallion.core.config import MedallionSettings
from service_kit.lakehouse import task_registry
from service_kit.lakehouse.task_registry import TaskRegistration


log = logging.getLogger(__name__)

#: The engine name this plane answers to. Kept beside the writer rather than in `service-kit`,
#: because the platform must never hold an engine vocabulary — that is the whole decoupling.
RAY_ENGINE = "ray"


def register_ray_tasks(settings: MedallionSettings) -> int:
    """Write every chart-declared task into ``<control_root>/_tasks/``; return how many landed.

    NON-FATAL, and the failure is legible rather than silent: a registration that cannot be written
    leaves the catalog's declaration door answering 422 naming the exact task that is missing, which
    is a better answer than a cascade head that refuses to start. Crashing here would take down
    ``/produce`` — the whole ingest surface — over a capability nothing has asked for yet.
    """
    if not settings.ray_tasks:
        return 0
    if not settings.control_root:
        log.error(
            "ray_tasks_unregisterable",
            extra={"tasks": len(settings.ray_tasks), "reason": "MEDALLION_CONTROL_ROOT is empty; transform declarations naming these tasks will be refused"},
        )
        return 0
    storage_options = settings.storage_options()
    landed = 0
    for declared in settings.ray_tasks:
        registration = TaskRegistration(
            task=declared.task,
            engine=RAY_ENGINE,
            command=declared.command,
            code_version=declared.code_version or settings.ray_code_version,
            cardinalities=declared.cardinalities,
            obligations=declared.obligations,
        )
        try:
            task_registry.put_task(settings.control_root, storage_options, registration)
        except Exception:
            # One unwritable record must not cost the others theirs — the tasks are independent, and
            # a partial registry refuses exactly the declarations it cannot honour.
            log.exception("ray_task_registration_failed", extra={"task": declared.task})
            continue
        landed += 1
    log.info("ray_tasks_registered", extra={"declared": len(settings.ray_tasks), "registered": landed, "control_root": settings.control_root})
    return landed
