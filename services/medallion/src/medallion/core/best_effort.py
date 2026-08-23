"""The compensating-emit guard, shared by every lane that reports a failure.

Every emit this wraps is deliberately best-effort, and the reasons are sound and written at each site: a
graph outage must not convert a correct refusal into a retry storm, and a FAIL record that cannot be
written must not stop the DROP that keeps a deterministic failure from re-reading every blob from S3
`maxDeliver` times. An activity that raises is retried and can end FAILED, so a lineage outage would
otherwise leave a workflow unable to finish reporting a failure — strictly worse. That part is design.

What WAS a defect is that `with suppress(Exception)` threw the diagnosis away with the exception. These
blocks ARE the compensating control, so a failure inside one produces exactly the silence the control
exists to prevent — and during a lineage or NATS outage that means every workflow failure in the window
is destroyed with nothing afterwards indicating a gap exists.

Worse than merely silent, on the reporting path: the `log.error` that follows these blocks still prints,
and `report_stage_outcome`'s docstring promises "THE FAILURE REACHES THE GRAPH, not just this log line".
So the log asserts a graph write that never happened. Logging costs nothing on the happy path and turns
an invisible failure into a searchable one.

Lives in `core/` because it has more than one consumer: `services/transform.py` (where it was written)
and `workflow.py`, whose four reporting sites were still bare.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager


log = logging.getLogger(__name__)


@contextmanager
def best_effort(what: str, **context: object) -> Iterator[None]:
    """Run a compensating emit that must not change the caller's control flow — and SAY SO if it fails."""
    try:
        yield
    except Exception:
        log.exception(f"medallion_best_effort_emit_failed_{what}", extra=dict(context))
