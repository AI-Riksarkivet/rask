"""The stage decorator — the decoratable seam for Ray Data pipeline stages.

``@stage("layout")`` wraps a callable in a child run of whatever parent is ambient
(the driver's :func:`~lineage_kit.runs.job_run` block) or carried in the environment
(``RASK_LINEAGE_CONTEXT``, for a stage body running in another Ray process). Each
invocation is one run: START before the call, COMPLETE after, FAIL (with the exception's
``errorMessage`` facet) and re-raise on error. While the body runs, ITS context is
ambient — so actors it constructs, or nested stages, parent onto the stage run.

Job names compose hierarchically: a stage under job ``ingest`` is ``ingest.layout``;
with no resolvable parent the stage runs as its own root (still emitted, still valid).
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, cast

from lineage_kit.context import resolve_context, use_context
from lineage_kit.emitter import use_emitter
from lineage_kit.runs import LineageRun


if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterable

    from lineage_kit.context import LineageContext
    from lineage_kit.emitter import Emitter
    from lineage_kit.runs import DatasetLike, OutputDatasetLike


def stage[**P, R](
    name: str | None = None,
    *,
    namespace: str | None = None,
    emitter: Emitter | None = None,
    inputs: Iterable[DatasetLike] = (),
    outputs: Iterable[OutputDatasetLike] = (),
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a pipeline-stage callable so every invocation emits a linked child run."""

    def decorate(fn: Callable[P, R]) -> Callable[P, R]:
        stage_name = name or getattr(fn, "__name__", "stage")

        def open_run() -> LineageRun:
            parent: LineageContext | None = resolve_context()
            job_name = f"{parent.job_name}.{stage_name}" if parent else stage_name
            if namespace is not None:
                ns = namespace
            elif parent is not None:
                ns = parent.namespace
            else:
                from lineage_kit.config import LineageSettings  # local: only to default the namespace

                ns = LineageSettings().namespace
            run = LineageRun(job_name=job_name, namespace=ns, parent=parent, emitter=emitter)
            run.start(inputs=inputs)
            return run

        # A coroutine function must be awaited INSIDE the run window — the sync wrapper
        # would emit COMPLETE at coroutine-creation time and could never observe a FAIL.
        if inspect.iscoroutinefunction(fn):
            afn = cast("Callable[P, Coroutine[object, object, R]]", fn)

            @functools.wraps(fn)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                run = open_run()
                with use_context(run.context), use_emitter(run.emitter):
                    try:
                        result = await afn(*args, **kwargs)
                    except Exception as exc:
                        run.fail(exc)
                        raise
                run.complete(outputs=outputs)
                return result

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            run = open_run()
            with use_context(run.context), use_emitter(run.emitter):
                try:
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    run.fail(exc)
                    raise
            run.complete(outputs=outputs)
            return result

        return wrapper

    return decorate
