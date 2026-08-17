"""The dummy lane's OpenLineage emission — provenance the platform can read back.

Modelled on ``scripts/ray_train_job.py``'s emitter, which is the estate's worked reference for a
run that a SERVICE executes on behalf of a person. The shape matters more than the values here: a
lane that emits nothing is invisible to the lineage graph, and a lane that emits the WRONG shape is
worse, because `notifiable()` discards it and acks SUCCESS — so nothing anywhere reports the loss.

**The human rides ``lance.originator``, never ``author``.** This job authenticates to the lineage
ingest as a service, and `enforce_author` overwrites the author facet with that service's verified
sub — deliberately, since honouring a producer-supplied author would let any producer file a row in
a named person's inbox. Setting `author` would therefore be silently discarded while looking handled.

**A non-personal principal is dropped rather than carried.** A role literal (`ray`, `data_eng`) or a
wildcard is not an address: carrying it writes into an inbox actor literally named `ray`. That is the
live defect in the medallion movers, and this lane must not reproduce it. Dropping the field loses
nothing — the event still records the run for the graph, it simply targets nobody, which is the
honest outcome for a run no person asked for.

Emission is BEST EFFORT and never raises into the transform: provenance must not fail a run that
actually produced data. A failure is loud on stderr, with the HTTP status kept so a 401/403
(credential problem) stays distinguishable from an outage.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

_PRODUCER = "https://github.com/AI-Riksarkivet/rask/runners/dummy"
_RUN_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_BASE_FACET = "https://openlineage.io/spec/1-0-5/OpenLineage.json#/$defs/BaseFacet"
_VERSION_FACET = "https://openlineage.io/spec/facets/1-0-0/DatasetVersionDatasetFacet.json#/$defs/DatasetVersionDatasetFacet"

#: The only states a person is told about. START/RUNNING notify nobody by product decision, and
#: RECONCILED is lineage's own repair marker rather than an outcome anyone chose.
TERMINAL_STATES = frozenset({"COMPLETE", "FAIL"})

#: Principals that are not an address. A role literal reaches an inbox actor named after the role;
#: a wildcard is a statement about everyone and therefore about no one.
_NOT_A_PERSON = frozenset({"", "*", "user:*", "ray", "data_eng", "analyst", "htr", "service", "system"})


def _dataset_ref(identifier: str) -> dict[str, str]:
    """Split a catalog identifier into the ``namespace``/``name`` pair lineage nodes are keyed by.

    ``name`` stays the FULL identifier — `silver$dummy`, never `dummy` — because delivery and render
    check `table:<name>` against the FGA object, and an unqualified name matches no tenant-qualified
    grant, which counts every recipient HIDDEN rather than denied.
    """
    return {"namespace": identifier.split("$", 1)[0], "name": identifier}


def build_run_event(
    *,
    event_type: str,
    run_id: str,
    to_id: str,
    from_id: str,
    rows: int = 0,
    version: int | None = None,
    originator: str = "",
    project: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """One terminal RunEvent for a dummy-lane run.

    ``version`` belongs on COMPLETE only — a FAIL committed nothing, and a fabricated version there
    would make a failed run look like it produced data.
    """
    if event_type not in TERMINAL_STATES:
        raise ValueError(f"{event_type!r} is not terminal; this lane emits only {sorted(TERMINAL_STATES)} (a START notifies nobody)")

    lance: dict[str, Any] = {
        "_producer": _PRODUCER,
        "_schemaURL": _BASE_FACET,
        "operation": "transform",
        "run_id": run_id,
        "rows": rows,
    }
    # Both are TARGETING hints and authorize nothing — the notifications plane re-derives every
    # recipient's visibility at delivery.
    if originator and originator not in _NOT_A_PERSON:
        lance["originator"] = originator
    if project:
        lance["project"] = project

    run_facets: dict[str, Any] = {"lance": lance}
    if error is not None:
        run_facets["errorMessage"] = {
            "_producer": _PRODUCER,
            "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json#/$defs/ErrorMessageRunFacet",
            "message": error[:1000],
            "programmingLanguage": "PYTHON",
        }

    output: dict[str, Any] = _dataset_ref(to_id)
    if version is not None:
        output["facets"] = {"version": {"_producer": _PRODUCER, "_schemaURL": _VERSION_FACET, "datasetVersion": str(version)}}

    return {
        "eventType": event_type,
        "eventTime": datetime.now(UTC).isoformat(),
        "producer": _PRODUCER,
        "schemaURL": _RUN_SCHEMA,
        "run": {"runId": run_id, "facets": run_facets},
        "job": {
            "namespace": "ray-jobs",
            "name": f"dummy.{to_id}",
            "facets": {
                "jobType": {
                    "_producer": _PRODUCER,
                    "_schemaURL": "https://openlineage.io/spec/facets/2-0-3/JobTypeJobFacet.json#/$defs/JobTypeJobFacet",
                    "processingType": "BATCH",
                    "integration": "RAY",
                    "jobType": "JOB",
                }
            },
        },
        # The DERIVED_FROM edge. Without an input, silver lands in the graph as an orphan nobody can
        # trace back to bronze — which is most of what a lineage graph is for.
        "inputs": [_dataset_ref(from_id)],
        "outputs": [output],
    }


def emit(event: dict[str, Any]) -> bool:
    """POST the event to the lineage ingest. Returns whether it landed; never raises.

    Ray pods carry no Dapr sidecar, so this is the plain HTTP ingest. Authentication is the SERVICE
    identity the job already has — the shared app token plus the bare FGA subject — which lineage
    verifies against its allowlist and then FGA-checks on the outputs, so this lane can only record
    provenance for what its rung permits. ``LINEAGE_URL`` unset means "not wired", not "failed".
    """
    url = os.environ.get("LINEAGE_URL", "").rstrip("/")
    if not url:
        return False
    headers = {"content-type": "application/json"}
    if service_token := os.environ.get("LINEAGE_SERVICE_TOKEN", ""):
        headers["dapr-api-token"] = service_token
        headers["x-lance-service-identity"] = os.environ.get("LINEAGE_SERVICE_ID", "")
    elif token := os.environ.get("LINEAGE_TOKEN", ""):
        headers["authorization"] = f"Bearer {token}"

    body = json.dumps(event).encode()
    for attempt in (1, 2):
        try:
            request = urllib.request.Request(f"{url}/api/v1/lineage", data=body, headers=headers)  # noqa: S310 — in-cluster URL from env
            urllib.request.urlopen(request, timeout=10)  # noqa: S310
        except urllib.error.HTTPError as exc:
            print(f"lineage-emit-failed attempt={attempt} status={exc.code} reason={exc.reason}", file=sys.stderr)
        # Broad on purpose: provenance must never crash a run that produced data.
        except Exception as exc:
            print(f"lineage-emit-failed attempt={attempt} error={exc}", file=sys.stderr)
        else:
            return True
    return False
