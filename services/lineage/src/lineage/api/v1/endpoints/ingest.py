"""OpenLineage HTTP ingest endpoint (``POST /api/v1/lineage``) for external producers.

The OpenLineage HTTP-transport default path: any producer (our emitter, Airflow, Spark, dbt, …)
configured with ``OPENLINEAGE_URL`` pointed here ingests with no glue — the lightweight-Marquez
contract. Durable catalog→lineage delivery rides the Dapr subscription (``lineage.api.dapr``) instead.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import enforce_author, enforce_output_authz
from lineage.api.security import CurrentToken
from lineage.core.metrics import Outcome, record_ingest_duration, record_outcome
from lineage.models import RunEvent
from lineage.services.consumer import record_event_best_effort


router = APIRouter(prefix="/api/v1", tags=["ingest"])


@router.post("/lineage", status_code=201)
async def ingest_event(event: RunEvent, request: Request, repository: RepositoryDep, settings: SettingsDep, token: CurrentToken) -> dict[str, str]:
    """Ingest one OpenLineage ``RunEvent`` into the lineage graph.

    This is the OpenLineage HTTP-transport default path, so any OpenLineage producer
    (our emitter, Airflow, Spark, dbt, …) configured with ``OPENLINEAGE_URL`` pointed
    here ingests with no glue — the lightweight-Marquez contract.

    When OIDC is enabled the ``CurrentToken`` dependency requires a verified bearer token
    (401 otherwise), :func:`~lineage.api.fga_deps.enforce_author` binds the run author to that
    token's subject (no self-asserted identity), and :func:`~lineage.api.fga_deps.enforce_output_authz`
    requires ``can_write_data`` on every output dataset (a producer can't record provenance for a table
    it can't write). Both are no-ops when auth is off.
    """
    enforce_author(event, token)
    await enforce_output_authz(event, request, settings, token)
    started = time.perf_counter()
    await repository.ingest_event(event)
    # The durable events feed is a secondary projection of the authoritative AGE graph (committed above);
    # the feed write is best-effort and shared with the JetStream consumer so both paths project alike.
    await record_event_best_effort(repository, event)
    # Domain metrics for the HTTP transport too — the trainer's whole lifecycle and every external
    # producer land here, so counting only the Dapr subscriber undercounted real ingest (audit 2026-07-15).
    # A rejected request (401/403/422) never reaches this point, matching the subscriber's DROPPED-vs-
    # INGESTED semantics; transport-level failures stay visible through the generic HTTP RED metrics.
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED)
    return {"status": "ingested", "run": event.run.run_id}
