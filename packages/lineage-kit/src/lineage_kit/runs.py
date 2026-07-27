"""The run state machine + the offline job entry shape.

A :class:`LineageRun` is one emitting run at any level of the hierarchy (job, stage, or
actor). It enforces the OpenLineage state contract: one START, then at most ONE terminal
event (COMPLETE / FAIL / ABORT) — a second terminal call is a logged no-op, so an
at-least-once caller cannot double-terminate a run.

:func:`job_run` is the OFFLINE entry shape — the driver of a ``ray job submit`` /
lance-ray pipeline wraps its work in it: START on entry, COMPLETE on clean exit, FAIL
(and re-raise) on exception. Inside the block the run's context is ambient, so
``@stage``-decorated callables and actors it launches link themselves automatically.
Online (Ray Serve) code uses :class:`~lineage_kit.actor.LineageActorMixin` instead.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lineage_kit.context import LineageContext, use_context
from lineage_kit.emitter import ambient_emitter, default_emitter, use_emitter
from lineage_kit.schemas import (
    Dataset,
    ErrorMessageRunFacet,
    Job,
    OutputDataset,
    Run,
    RunEvent,
    RunFacets,
    RunState,
)


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from lineage_kit.emitter import Emitter

log = logging.getLogger(__name__)

#: uuid5 namespace for deterministic run ids (this package's own derivation).
_RUN_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/Borg93/rask/packages/lineage-kit")

#: Coercible dataset forms: a model, or a bare ``(namespace, name)`` pair.
type DatasetLike = Dataset | tuple[str, str]
type OutputDatasetLike = OutputDataset | tuple[str, str]


def run_id_for(seed: str) -> str:
    """A spec-valid UUID run id that is STABLE for ``seed`` — so an at-least-once
    redelivery MERGEs onto the same run instead of duplicating it. Keep the
    human-readable seed as the job name for correlation, never as the id itself."""
    return str(uuid.uuid5(_RUN_ID_NAMESPACE, seed))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _inputs(datasets: Iterable[DatasetLike]) -> list[Dataset]:
    return [d if isinstance(d, Dataset) else Dataset(namespace=d[0], name=d[1]) for d in datasets]


def _outputs(datasets: Iterable[OutputDatasetLike]) -> list[OutputDataset]:
    return [d if isinstance(d, OutputDataset) else OutputDataset(namespace=d[0], name=d[1]) for d in datasets]


class LineageRun:
    """One run at any level: emits its own events, hands children a serializable context."""

    def __init__(
        self,
        *,
        job_name: str,
        namespace: str,
        run_id: str | None = None,
        parent: LineageContext | None = None,
        emitter: Emitter | None = None,
    ) -> None:
        self.job_name = job_name
        self.namespace = namespace
        self.run_id = run_id or str(uuid.uuid4())
        self.parent = parent
        self._emitter = emitter
        self._terminal: RunState | None = None

    @property
    def emitter(self) -> Emitter:
        """Explicit injection wins, then the enclosing run's emitter, then the env default."""
        if self._emitter is not None:
            return self._emitter
        ambient = ambient_emitter()
        return ambient if ambient is not None else default_emitter()

    @property
    def configured_emitter(self) -> Emitter | None:
        """The explicitly injected emitter, if any — children inherit this, not the resolved default."""
        return self._emitter

    @property
    def pending(self) -> bool:
        """True until a terminal event (COMPLETE/FAIL/ABORT) has been emitted."""
        return self._terminal is None

    @property
    def context(self) -> LineageContext:
        """The serializable context children carry — root propagates from the parent."""
        if self.parent is not None:
            return self.parent.child(job_name=self.job_name, run_id=self.run_id, namespace=self.namespace)
        return LineageContext.root(namespace=self.namespace, job_name=self.job_name, run_id=self.run_id)

    def _event(
        self,
        state: RunState,
        *,
        inputs: Iterable[DatasetLike] = (),
        outputs: Iterable[OutputDatasetLike] = (),
        error: ErrorMessageRunFacet | None = None,
    ) -> RunEvent:
        facets = RunFacets(parent=self.parent.parent_facet() if self.parent else None, error_message=error)
        return RunEvent(
            event_type=state,
            event_time=_now(),
            run=Run(run_id=self.run_id, facets=facets),
            job=Job(namespace=self.namespace, name=self.job_name),
            inputs=_inputs(inputs),
            outputs=_outputs(outputs),
        )

    def _emit_terminal(
        self,
        state: RunState,
        *,
        inputs: Iterable[DatasetLike] = (),
        outputs: Iterable[OutputDatasetLike] = (),
        error: ErrorMessageRunFacet | None = None,
    ) -> RunEvent | None:
        if self._terminal is not None:
            log.warning(
                "lineage_terminal_repeat: run %s (%s) already terminated %s; dropping %s",
                self.run_id,
                self.job_name,
                self._terminal.value,
                state.value,
            )
            return None
        self._terminal = state
        event = self._event(state, inputs=inputs, outputs=outputs, error=error)
        self.emitter.emit(event)
        return event

    def start(self, *, inputs: Iterable[DatasetLike] = (), outputs: Iterable[OutputDatasetLike] = ()) -> RunEvent:
        """Emit START (call once, when the unit of work begins)."""
        event = self._event(RunState.START, inputs=inputs, outputs=outputs)
        self.emitter.emit(event)
        return event

    def running(self, *, inputs: Iterable[DatasetLike] = (), outputs: Iterable[OutputDatasetLike] = ()) -> RunEvent:
        """Emit a RUNNING heartbeat (non-terminal, repeatable)."""
        event = self._event(RunState.RUNNING, inputs=inputs, outputs=outputs)
        self.emitter.emit(event)
        return event

    def complete(self, *, inputs: Iterable[DatasetLike] = (), outputs: Iterable[OutputDatasetLike] = ()) -> RunEvent | None:
        """Emit COMPLETE — the success terminal. No-op (logged) if already terminated."""
        return self._emit_terminal(RunState.COMPLETE, inputs=inputs, outputs=outputs)

    def fail(
        self,
        error: str | BaseException,
        *,
        inputs: Iterable[DatasetLike] = (),
        outputs: Iterable[OutputDatasetLike] = (),
    ) -> RunEvent | None:
        """Emit FAIL with an ``errorMessage`` facet (message + stack trace for exceptions)."""
        if isinstance(error, BaseException):
            facet = ErrorMessageRunFacet(
                message=f"{type(error).__name__}: {error}",
                stack_trace="".join(traceback.format_exception(error)),
            )
        else:
            facet = ErrorMessageRunFacet(message=error)
        return self._emit_terminal(RunState.FAIL, inputs=inputs, outputs=outputs, error=facet)

    def abort(self, reason: str | None = None) -> RunEvent | None:
        """Emit ABORT — the cancelled terminal (an operator stop, a per-chunk stop, a shutdown)."""
        facet = ErrorMessageRunFacet(message=reason) if reason else None
        return self._emit_terminal(RunState.ABORT, error=facet)


@contextmanager
def job_run(
    job_name: str,
    *,
    namespace: str | None = None,
    run_id: str | None = None,
    parent: LineageContext | None = None,
    emitter: Emitter | None = None,
    inputs: Iterable[DatasetLike] = (),
    outputs: Iterable[OutputDatasetLike] = (),
) -> Iterator[LineageRun]:
    """The offline entry shape: START on entry, COMPLETE on exit, FAIL + re-raise on error.

    The run's context is ambient inside the block, so ``@stage`` callables and
    mixin actors launched from it link themselves as children. A run the body already
    terminated (``run.abort()`` / ``run.complete()``) is left alone on exit.
    """
    from lineage_kit.config import LineageSettings  # local: only needed to default the namespace

    run = LineageRun(
        job_name=job_name,
        namespace=namespace or LineageSettings().namespace,
        run_id=run_id,
        parent=parent,
        emitter=emitter,
    )
    run.start(inputs=inputs)
    with use_context(run.context), use_emitter(run.emitter):
        try:
            yield run
        except Exception as exc:
            run.fail(exc)
            raise
    if run.pending:
        run.complete(outputs=outputs)
