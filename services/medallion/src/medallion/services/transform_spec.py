"""Resolve the DECLARED transform a mover runs — the read half of the transform-spec contract.

The catalog writes a :class:`~service_kit.lakehouse.transform_specs.TransformSpec` through an
admin-gated door; this is where a mover reads one back. Object-store-backed, so a mover pod that has
never met the catalog resolves the record with nothing but the control root — no catalog client on
the submit path, which is the same reason the maintenance sweep reads policies directly.

**Opt-in, and the default is load-bearing.** ``MEDALLION_TRANSFORM`` unset means this returns ``None`` and
the chart's ``ray_entrypoint``/``ray_job_params``/``ray_code_version`` govern exactly as before. An
estate that has declared nothing is unchanged rather than quietly running under a new scheme — the
stance ``ray_code_version`` already takes.

**A named-but-undeclared transform REFUSES.** It must never fall back to the chart entrypoint, because
that is precisely the failure the record exists to eliminate: the mover would run the OLD program
while an operator believes the declaration governs it, and nothing anywhere would be red. The
refusal is the mover's form of the door's 422 — same sentence, same named key.
"""

from __future__ import annotations

import logging
from typing import Protocol

from fastapi.concurrency import run_in_threadpool

from service_kit.lakehouse import transform_specs
from service_kit.lakehouse.transform_specs import TransformSpec


log = logging.getLogger(__name__)


class UndeclaredTransformError(RuntimeError):
    """A mover is configured for a transform the catalog has no declaration for.

    Distinct from a submit failure so the caller can tell "the cluster refused my job" from "nobody
    declared what this mover should run" — the second is an operator action, not a retryable fault of
    the run.
    """


class _TransformSettings(Protocol):
    """The three fields resolution needs — a Protocol so tests need no full Settings object."""

    transform: str
    control_root: str

    def storage_options(self) -> dict[str, str]: ...


def resolve_transform(settings: _TransformSettings, *, project: str) -> TransformSpec | None:
    """The declared spec for this mover's transform, or ``None`` when none is configured.

    Raises :class:`UndeclaredTransformError` when a transform IS named but cannot be resolved — including when
    the control root is unconfigured, because "not declared" and "cannot look" need opposite answers
    and a reader that conflated them would report a misconfigured mover as an undeclared transform.
    """
    name = getattr(settings, "transform", "")
    if not name:
        return None
    if not settings.control_root:
        raise UndeclaredTransformError(
            f"transform {name!r} is configured but MEDALLION_CONTROL_ROOT is empty — the declaration cannot be read. "
            "Set it to the catalog's control root (the same one the catalog writes _transforms/ under)."
        )
    if not project:
        raise UndeclaredTransformError(
            f"transform {name!r} is configured but this run carries no project — a transform is keyed (project, name), "
            "and defaulting the tenant would run one project's transform over another's bytes."
        )
    spec = transform_specs.get_spec(settings.control_root, settings.storage_options(), project, name)
    if spec is None:
        raise UndeclaredTransformError(
            f"no transform is declared as {name!r} in project {project!r}; "
            f"declare it first via POST /v1/project/{project}/transform/set. "
            "Refusing rather than falling back to the chart entrypoint — a fallback would run the old "
            "program under the declaration's name."
        )
    return spec


async def resolve_transform_async(settings: _TransformSettings, *, project: str) -> TransformSpec | None:
    """:func:`resolve_transform` off the event loop — the object-store read is blocking."""
    if not getattr(settings, "transform", ""):
        return None  # no IO to do; skip the threadpool hop entirely
    return await run_in_threadpool(resolve_transform, settings, project=project)
