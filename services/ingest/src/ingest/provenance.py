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

**And REFUSED is not unknown either — the false negative this module shipped with.** The read was
sent with NO credentials, so on an auth-enabled estate the graph answered 401, the bare
`except Exception` swallowed it as "unreachable", and the endpoint reported no defect. Measured:
four `401 Unauthorized` from the emitter in five minutes while that same run reported
`{"status": "COMPLETE", "defect": null}` and the lane printed `OK  A8 — no provenance defect`. A8
certified provenance as sound while EVERY lineage event was being rejected.

A gate that passes when the thing it guards is entirely broken is worse than no gate — it turns a
loud failure into a silent one and teaches the reader to trust it. So a 401/403 is now its own
answer: the graph is reachable and telling us we may not look. That is a MISCONFIGURATION, reported
as such, and distinct from both "no provenance" and "could not ask".
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict

from ingest.config import settings


logger = logging.getLogger(__name__)

#: How long the status endpoint will wait on the graph. Short on purpose: this is a per-request
#: join on an endpoint an operator reaches for when something is already wrong, and it must not
#: inherit the latency of the system it is reporting on.
TIMEOUT_SECONDS = 2.0

#: Ceiling on the memo of runs already found in the graph. An ingest pod serves runs indefinitely, so
#: an unmemoized answer is one full board download per poll and an UNBOUNDED memo is a leak for the
#: lifetime of the pod — the same pair of failures `lineage.py`'s event ring buffer had to settle.
#: Insertion-ordered with the oldest evicted: the runs being polled are the recent ones, and evicting
#: a settled run costs one extra board read the next time somebody asks about it.
MEMO_MAX_RUNS = 1024


def lineage_base_url() -> str:
    """Where the lineage service lives. Config-driven like every other upstream in the fleet."""
    return settings().lineage_url.rstrip("/")


#: The credentials the READ needs on a governed estate — the same pair the EMITTER uses, because
#: reading the graph and writing to it are the same door. Sending neither is what made a refusal
#: indistinguishable from an outage.
def _service_headers() -> dict[str, str]:
    config = settings()
    token, identity = config.lineage_app_token, config.lineage_service_identity
    if not token or not identity:
        # HALF-CONFIGURED is worth nothing: the lineage door needs BOTH, and sending one is a request
        # that will be refused for a reason nobody can see from here. The emitter warns about exactly
        # this pair; a matching silence here would repeat the defect on the read side.
        return {}
    return {"dapr-api-token": token, "x-lance-service-identity": identity}


class ProvenanceRefused(Exception):
    """The graph was reachable and REFUSED the read — a misconfiguration, not an outage.

    Its own exception rather than a `None`, because the two demand opposite responses from an
    operator: a `None` says "try again / check the service is up", while this says "this service has
    no valid lineage credential and its provenance claims cannot be trusted".
    """


class LineageProvenanceReader:
    """Answers "is this ingest run in the graph?" against the lineage service's runs board.

    **A PRESENT run is remembered.** The board is the estate's whole run list — the endpoint takes no
    run-id filter and no page (`services/lineage/.../endpoints/runs.py`) — so answering this question
    costs one download of every run the caller may see, plus a linear scan. `GET /ingests/{run_id}`
    asks it on every read of a COMPLETE run, and the compute zone POLLS that endpoint, so a finished
    run re-downloaded the whole board every couple of seconds for as long as anyone had its page open
    (ING-14).

    Only the TRUE answer is memoized, and that asymmetry is the point: a run present in the graph is
    present permanently, while "absent" is a snapshot of a race — the ingest run reaches COMPLETE
    before its terminal event has necessarily landed — and "could not ask" is not an answer at all.
    Caching either of those would freeze a transient state into the defect A8 reports.
    """

    def __init__(self) -> None:
        #: Lineage run ids already found in the graph, oldest first. Bounded, and read/written under
        #: `_lock` because `has_run` is called through `asyncio.to_thread` from the status route.
        self._present: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def memoized(self) -> int:
        """How many settled runs this reader is currently remembering. For the bound's own test."""
        with self._lock:
            return len(self._present)

    def has_run(self, run_id: str) -> bool | None:
        """True / False / None, where None means "the graph could not be asked".

        Raises `ProvenanceRefused` when the graph answers 401/403 — see the module docstring.
        """
        from ingest.http import shared_client
        from ingest.lineage import lineage_run_id

        target = lineage_run_id(run_id)
        with self._lock:
            if target in self._present:
                return True
        try:
            # `/runs`, at the service ROOT. The lineage service mounts its v1 routers without a
            # version prefix — the gateway supplies `/api/lineage` and the pod serves `/runs`
            # (confirmed against the live pod's own openapi.json, which also puts OpenLineage
            # ingestion at `/api/v1/lineage`). Guessing `/v1/runs` from the module layout returns a
            # 404, which this method's except-branch would have reported as "graph unreachable" —
            # a wrong path and a down service would have been indistinguishable.
            response = shared_client().get(f"{lineage_base_url()}/runs", headers=_service_headers(), timeout=TIMEOUT_SECONDS)
            # BEFORE raise_for_status, so a refusal never reaches the generic handler below. This is
            # the whole fix: a 401 used to fall into `except Exception` and be reported as an outage,
            # which the endpoint then rendered as "no defect".
            if response.status_code in (401, 403):
                logger.warning(
                    "lineage refused the provenance read (%s) — this service's A8 verdict is not trustworthy "
                    "until it has a lineage credential (RASK_LINEAGE_APP_TOKEN + RASK_LINEAGE_SERVICE_IDENTITY)",
                    response.status_code,
                )
                raise ProvenanceRefused(f"lineage refused the provenance read with {response.status_code}")
            response.raise_for_status()
            runs = response.json().get("runs") or []
        except ProvenanceRefused:
            raise
        except Exception:
            logger.debug("lineage graph unreachable while resolving run %s", run_id, exc_info=True)
            return None
        found = any(isinstance(run, dict) and run.get("run_id") == target for run in runs)
        if found:
            with self._lock:
                self._present[target] = None
                while len(self._present) > MEMO_MAX_RUNS:
                    self._present.popitem(last=False)
        return found
