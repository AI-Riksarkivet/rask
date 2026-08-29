"""The actor mixin — the inheritable seam for Ray actors and Ray Serve deployments.

OFFLINE (an actor inside a submitted pipeline): the driver hands the stage/job context
across the process boundary — either as a constructor argument
(``MyActor.remote(lineage_context=ctx.model_dump_json())``) or via the environment
(``runtime_env={"env_vars": ctx.as_env()}``) — and the actor calls
``self.init_lineage("layout", context=...)`` in ``__init__``. The actor's run is then a
child of the carried run, with the job run as root.

ONLINE (a long-lived Serve deployment): no parent exists — ``init_lineage`` with no
context opens a ROOT run for the replica's lifetime, and per-request/per-batch work is
emitted as child runs via :meth:`LineageActorMixin.lineage_task`.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from lineage_kit.context import child_job_name, coerce_context, resolve_context, resolve_namespace, use_context
from lineage_kit.emitter import use_emitter
from lineage_kit.runs import LineageRun


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from lineage_kit.context import LineageContext
    from lineage_kit.emitter import Emitter
    from lineage_kit.runs import DatasetLike, OutputDatasetLike


class LineageActorMixin:
    """Inherit in any Ray actor or Serve deployment class; call :meth:`init_lineage` in ``__init__``."""

    _lineage_run: LineageRun | None = None

    def init_lineage(
        self,
        actor_name: str | None = None,
        *,
        context: LineageContext | Mapping[str, object] | str | None = None,
        namespace: str | None = None,
        emitter: Emitter | None = None,
        start: bool = True,
    ) -> LineageRun:
        """Open this actor's run. ``context`` accepts a model, dict, or JSON string; when
        omitted, the ambient context or ``RASK_LINEAGE_CONTEXT`` is used; when neither
        exists the actor runs as its own root (the online Serve shape)."""
        parent = coerce_context(context) or resolve_context()
        name = actor_name or type(self).__name__.lower()
        run = LineageRun(
            job_name=child_job_name(parent, name),
            namespace=resolve_namespace(namespace, parent),
            parent=parent,
            emitter=emitter,
        )
        if start:
            run.start()
        self._lineage_run = run
        return run

    @property
    def lineage(self) -> LineageRun:
        """This actor's run (:meth:`init_lineage` must have been called)."""
        if self._lineage_run is None:
            msg = "init_lineage() was never called on this actor — call it in __init__"
            raise RuntimeError(msg)
        return self._lineage_run

    @contextmanager
    def lineage_task(
        self,
        name: str,
        *,
        inputs: Iterable[DatasetLike] = (),
        outputs: Iterable[OutputDatasetLike] = (),
        run_id: str | None = None,
        emitter: Emitter | None = None,
    ) -> Iterator[LineageRun]:
        """One unit of actor work (a request, a batch) as a child run of the actor's run:
        START on entry, COMPLETE on clean exit, FAIL + re-raise on exception."""
        run = LineageRun(
            job_name=f"{self.lineage.job_name}.{name}",
            namespace=self.lineage.namespace,
            run_id=run_id,
            parent=self.lineage.context,
            emitter=emitter if emitter is not None else self.lineage.configured_emitter,
        )
        run.start(inputs=inputs)
        # The task's context (and emitter) are ambient inside the block — a @stage callable
        # or a nested actor opened here parents onto THIS task run, mirroring job_run.
        # contextvars are asyncio-task-local, so concurrent Serve requests do not cross-link.
        with use_context(run.context), use_emitter(run.emitter):
            try:
                yield run
            except Exception as exc:
                run.fail(exc)
                raise
        if run.pending:
            run.complete(outputs=outputs)
