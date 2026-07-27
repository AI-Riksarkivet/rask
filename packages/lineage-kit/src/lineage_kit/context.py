"""The serializable parent-context carry — how job→stage→actor linkage crosses processes.

A :class:`LineageContext` is a small pydantic model naming a run (namespace, job name,
run id) plus the ROOT run of its hierarchy. It travels as JSON: pass
``ctx.model_dump_json()`` in a Ray actor's constructor args, or ``ctx.as_env()`` in a
``runtime_env``'s ``env_vars`` (the ``RASK_LINEAGE_CONTEXT`` variable), and rehydrate on
the other side with :func:`coerce_context` / :meth:`LineageContext.from_env`. Ambient
in-process carry uses a :mod:`contextvars` variable (:func:`use_context` /
:func:`current_context`), which is what the ``@stage`` decorator reads.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Self

from pydantic import BaseModel

from lineage_kit.schemas import JobRef, ParentRunFacet, RootRef, RunRef


#: The env var a serialized context travels in across a process boundary
#: (Ray ``runtime_env.env_vars``, a Serve deployment's env, a subprocess).
CONTEXT_ENV_VAR = "RASK_LINEAGE_CONTEXT"


class LineageContext(BaseModel):
    """A run's identity + its hierarchy root — everything a child needs to link itself."""

    namespace: str
    job_name: str
    run_id: str
    root_namespace: str
    root_job_name: str
    root_run_id: str

    @classmethod
    def root(cls, *, namespace: str, job_name: str, run_id: str) -> Self:
        """The context of a TOPMOST run (its own root) — the offline job driver's shape."""
        return cls(
            namespace=namespace,
            job_name=job_name,
            run_id=run_id,
            root_namespace=namespace,
            root_job_name=job_name,
            root_run_id=run_id,
        )

    def child(self, *, job_name: str, run_id: str, namespace: str | None = None) -> LineageContext:
        """The context a run parented on THIS one hands to its own children (same root)."""
        return LineageContext(
            namespace=namespace or self.namespace,
            job_name=job_name,
            run_id=run_id,
            root_namespace=self.root_namespace,
            root_job_name=self.root_job_name,
            root_run_id=self.root_run_id,
        )

    def parent_facet(self) -> ParentRunFacet:
        """The ``parent`` run facet a CHILD of this run must carry (parent=this, root=this root)."""
        return ParentRunFacet(
            run=RunRef(run_id=self.run_id),
            job=JobRef(namespace=self.namespace, name=self.job_name),
            root=RootRef(
                run=RunRef(run_id=self.root_run_id),
                job=JobRef(namespace=self.root_namespace, name=self.root_job_name),
            ),
        )

    def as_env(self) -> dict[str, str]:
        """The env-var form for a ``runtime_env``'s ``env_vars`` / a subprocess environment."""
        return {CONTEXT_ENV_VAR: self.model_dump_json()}

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> LineageContext | None:
        """Rehydrate from ``RASK_LINEAGE_CONTEXT``, or ``None`` when it is not set."""
        raw = (environ if environ is not None else os.environ).get(CONTEXT_ENV_VAR)
        return cls.model_validate_json(raw) if raw else None


def coerce_context(value: LineageContext | Mapping[str, object] | str | None) -> LineageContext | None:
    """Accept a context in any carried form — model, dict, or JSON string."""
    if value is None or isinstance(value, LineageContext):
        return value
    if isinstance(value, str):
        return LineageContext.model_validate_json(value)
    return LineageContext.model_validate(value)


_current: ContextVar[LineageContext | None] = ContextVar("lineage_kit_context", default=None)


def current_context() -> LineageContext | None:
    """The ambient in-process context, if a run has been opened around this code."""
    return _current.get()


def resolve_context(explicit: LineageContext | Mapping[str, object] | str | None = None) -> LineageContext | None:
    """Resolution order for a parent: explicit argument → ambient contextvar → env var."""
    return coerce_context(explicit) or current_context() or LineageContext.from_env()


@contextmanager
def use_context(ctx: LineageContext) -> Iterator[LineageContext]:
    """Make ``ctx`` the ambient context for the enclosed block."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
