"""OpenLineage HTTP ingest endpoint (``POST /api/v1/lineage``) for external producers.

The OpenLineage HTTP-transport default path: any producer (our emitter, Airflow, Spark, dbt, …)
configured with ``OPENLINEAGE_URL`` pointed here ingests with no glue — the lightweight-Marquez
contract. Durable catalog→lineage delivery rides the Dapr subscription (``lineage.api.dapr``) instead.

**THIS DOOR RECORDS; IT DOES NOT TRIGGER.** An event ingested here lands in AGE and the durable feed
and drives NOTHING — the whole handler is authorize, ``ingest_event``, return, with no publish
anywhere on the path. The medallion cascade's head is a Dapr subscription on
the ``lineage.events.v1`` TOPIC (``medallion.api.bronze_arrival``), and nothing republishes from HTTP
ingest onto that topic.

The distinction is easy to miss and expensive: this is the one door an external orchestrator finds,
and the sentence above markets it to exactly the tools that would expect a write here to start a run.
It does not. The result is a perfect graph, zero compute, every pod green, and nothing to grep for.
An external producer that means to DRIVE the cascade writes its rows through the catalog's data-plane
door (``POST /v1/table/{id}/insert``), whose own emitter announces the write onto the topic — which is
what ``services/ingest`` does, and why it publishes no lineage event of its own.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from fastapi.exceptions import RequestValidationError
from lance_namespace import PermissionDeniedError
from pydantic import ValidationError

from lineage.api.dependencies import RepositoryDep, SettingsDep
from lineage.api.fga_deps import enforce_author, enforce_output_authz
from lineage.api.security import CurrentToken
from lineage.core.metrics import Door, Outcome, record_ingest_duration, record_outcome
from lineage.models import DatasetEvent, RunEvent, UnauthoredRunError, UngovernedOutputError, parse_event


# Unversioned like every sibling router — the composition layer (api/v1/router.py) mounts this one
# under /api/v1, the OpenLineage HTTP-transport default path the docstring above names.
router = APIRouter(tags=["ingest"])


@router.post("/lineage", status_code=201)
async def ingest_event(
    body: Annotated[dict[str, Any], Body()], request: Request, repository: RepositoryDep, settings: SettingsDep, token: CurrentToken
) -> dict[str, str | None]:
    """Ingest one OpenLineage event — a ``RunEvent``, or a ``DatasetEvent`` for a change no job performed.

    PARSED HERE RATHER THAN BY THE SIGNATURE, so this door and the bus door share ONE discriminator
    (`models.parse_event`). Declaring the body as `RunEvent` is what made a static event 422 before
    any handler ran: [[LIN-004]] moved catalog DDL onto `DatasetEvent` and taught only the bus. The
    catalog's HTTP emitter is best-effort, so every create it sent lost its provenance in silence —
    measured 2026-09-24, three creates, `{"events":[]}` in the durable feed, and a governance
    assertion reading `expected lineage creator=..., got None`.

    A body that is neither shape raises `RequestValidationError`, which `install_problem_handlers`
    renders exactly as FastAPI's own 422 — the wire answer for a malformed payload is unchanged.

    This is the OpenLineage HTTP-transport default path, so any OpenLineage producer
    (our emitter, Airflow, Spark, dbt, …) configured with ``OPENLINEAGE_URL`` pointed
    here ingests with no glue — the lightweight-Marquez contract.

    When OIDC is enabled the ``CurrentToken`` dependency requires a verified bearer token
    (401 otherwise), :func:`~lineage.api.fga_deps.enforce_author` binds the run author to that
    token's subject (no self-asserted identity), and :func:`~lineage.api.fga_deps.enforce_output_authz`
    requires ``can_write_data`` on every output dataset (a producer can't record provenance for a table
    it can't write). Both are no-ops when auth is off.
    """
    # A REFUSAL IS COUNTED HERE, not left to the status code. This is the ONLY door for a producer with
    # no Dapr sidecar — the whole Ray lane, every runner, and any external OpenLineage producer — so a
    # refusal here is a governed write whose run never reaches the graph while the producer exits
    # successfully. `Outcome.REFUSED` exists because that loss "can be a silent, deliberate loss, so it
    # gets its own alert"; counting it only on the subscriber made the same loss alertable on one door
    # and invisible on the other.
    #
    # THE HTTP STATUS CANNOT STAND IN FOR IT. This service answers 403 for ordinary reads too — a
    # `can_get_metadata` denial on a GET is one — so an undifferentiated status-code count cannot
    # separate a reader being told no from a producer's provenance being dropped.
    #
    # CLASSIFIED EXACTLY AS `services/consumer.py` DOES, and pinned against it by
    # `tests/unit/test_both_lineage_doors_count_the_same_loss.py`: a denied grant is repairable by
    # writing a tuple, while an ungoverned output and an unauthored run are repairable by nothing. Two
    # doors that disagreed about which is which would make the alert mean different things by route.
    try:
        event: RunEvent | DatasetEvent = parse_event(body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    try:
        enforce_author(event, token)
        await enforce_output_authz(event, request, settings, token)
    except (UnauthoredRunError, UngovernedOutputError):
        record_outcome(Outcome.UNREPAIRABLE, door=Door.HTTP)
        raise
    except PermissionDeniedError:
        record_outcome(Outcome.REFUSED, door=Door.HTTP)
        raise
    started = time.perf_counter()
    # ONE transaction: the AGE graph and the durable /events row. A feed write that fails takes the
    # ingest down with it, so the caller retries and the two can never disagree (see `ingest_event`).
    # TWO DOORS, exactly as `services/consumer.py` routes them, because a static metadata change has
    # no run and no job to record. Routing it through the run door is what mints a `(:Job)` for an
    # operation nobody performed, one per table per operation — and the `/jobs` fold makes that Job's
    # output set an access handle, so a phantom is an access-control object, not merely untidy.
    if isinstance(event, DatasetEvent):
        await repository.ingest_dataset_event(event)
    else:
        await repository.ingest_event(event)
    # Domain metrics for the HTTP transport too — the trainer's whole lifecycle and every external
    # producer land here, so counting only the Dapr subscriber undercounted real ingest (audit 2026-07-15).
    # A 401 or a 422 still never reaches this point and needs no domain outcome: neither names a run the
    # graph should have held. An authorization refusal does, and is counted above.
    record_ingest_duration(time.perf_counter() - started)
    record_outcome(Outcome.INGESTED, door=Door.HTTP)
    # `run` is null for a static change rather than an invented id: there was no run, and the
    # OpenLineage HTTP transport reads the status, not this field.
    return {"status": "ingested", "run": event.run_id}
