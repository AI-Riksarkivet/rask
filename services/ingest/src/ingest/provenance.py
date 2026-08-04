"""Does this run exist in the lineage graph? — the read half of A8.

A8 says *"a green sync with no lineage edge is a bug the UI should surface"*. Surfacing it needs two
halves, and only one of them was built: `RunRecord.is_defective` computed the verdict from a
`lineage_run_present` flag that nothing ever set. So the flag was False for every run, and the first
green in-cluster lane reported a provenance defect on a run whose lineage had simply never been
looked up.

That is a worse failure than not having the check. A gate that fires on every run is a gate an
operator learns to ignore, and then the one real provenance hole passes unnoticed among the false
ones. Either the check asks the graph or it should not claim to.

So this asks the graph — the lineage service's own runs board, which folds each run's current state
onto its `(:Run)` node in AGE and is therefore durable and shared across replicas rather than
process-local.

**Absent is not the same as unknown.** If the lineage service cannot be reached, this returns None
and the status endpoint reports NO defect. An unreachable graph means we do not know whether
provenance exists; claiming a defect from ignorance is how the check loses its meaning. A run whose
lineage is genuinely missing is answered by a reachable graph that does not contain it.
"""

from __future__ import annotations

import logging
import os


logger = logging.getLogger(__name__)

#: How long the status endpoint will wait on the graph. Short on purpose: this is a per-request
#: join on an endpoint an operator reaches for when something is already wrong, and it must not
#: inherit the latency of the system it is reporting on.
TIMEOUT_SECONDS = 2.0


def lineage_base_url() -> str:
    """Where the lineage service lives. Env-driven like every other upstream in the fleet."""
    return os.getenv("RASK_LINEAGE_URL", "http://rask-lineage:8000").rstrip("/")


class LineageProvenanceReader:
    """Answers "is this ingest run in the graph?" against the lineage service's runs board."""

    def has_run(self, run_id: str) -> bool | None:
        """True / False / None, where None means "the graph could not be asked"."""
        import httpx

        from ingest.lineage import lineage_run_id

        target = lineage_run_id(run_id)
        try:
            # `/runs`, at the service ROOT. The lineage service mounts its v1 routers without a
            # version prefix — the gateway supplies `/api/lineage` and the pod serves `/runs`
            # (confirmed against the live pod's own openapi.json, which also puts OpenLineage
            # ingestion at `/api/v1/lineage`). Guessing `/v1/runs` from the module layout returns a
            # 404, which this method's except-branch would have reported as "graph unreachable" —
            # a wrong path and a down service would have been indistinguishable.
            response = httpx.get(f"{lineage_base_url()}/runs", timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            runs = response.json().get("runs") or []
        except Exception:
            logger.debug("lineage graph unreachable while resolving run %s", run_id, exc_info=True)
            return None
        return any(isinstance(run, dict) and run.get("run_id") == target for run in runs)
