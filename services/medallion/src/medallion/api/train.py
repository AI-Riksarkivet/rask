"""``POST /train`` head + the ``/train-trigger`` subscription (#115a, docs/RAY-TRAIN.md D1/D2).

Training gets its OWN topic and consumer: the trigger is fire-and-track (submit-and-ack), never a
stage hop — see the design doc for why a workload-type field on the medallion trigger was rejected.
"""

from __future__ import annotations

from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.api.produce_auth import authorize_train
from medallion.core.config import get_settings
from medallion.services.train import (
    DATASET_PATTERN,
    MAX_FEATURES,
    MODEL_PATTERN,
    handle_train_trigger,
    submit_train_request,
    train_head_enabled,
)
from service_kit.governed.dapr_auth import require_dapr_token


router = APIRouter(tags=["train"])


class FeatureRef(BaseModel):
    """One training input: a ``stage$name`` feature dataset, optionally pinned to an exact version."""

    dataset: str = Field(pattern=DATASET_PATTERN)
    version: int | None = None


class TrainRequest(BaseModel):
    """The ``POST /train`` body — pointers only (claim-check): names, pins, and a SMALL config.

    Name shapes and the feature cap mirror the CONSUMER's validation exactly (review 2026-07-11):
    a request the consumer would DROP is refused HERE with a 422, never 202'd into a silent no-op.
    """

    model: str = Field(pattern=MODEL_PATTERN)
    features: list[FeatureRef] = Field(min_length=1, max_length=MAX_FEATURES)
    config: dict[str, Any] = Field(default_factory=dict)


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        headers={"Retry-After": "5"} if status == 503 else None,
        content={
            "type": f"https://lance.org/problems/{title.lower()}",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


@router.post("/train", status_code=202, response_model=None)  # union with JSONResponse → no auto model
async def train(
    body: TrainRequest,
    dapr: DaprClientDep,
    settings: SettingsDep,
    _: Annotated[None, Depends(authorize_train)],  # #64: service token OR an admin of the CONFIGURED
    # project — pinned, a stray ?project= is ignored (single-tenant write; see authorize_train)
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")] = None,
) -> dict[str, Any] | JSONResponse:
    """Request a training run: pin feature versions (omitted → LATEST, resolved HERE) and publish the
    training trigger — 202 with the correlation ``token``. Token-guarded like ``/produce``; a disabled
    head (no Ray path / S3 / bronze URI) is an explicit 409, a lost trigger an explicit 503 — never a 202
    that silently trains nothing."""
    if not train_head_enabled(settings):
        return _problem(409, "Conflict", "train head not configured (needs ray_enabled + S3 + bronze URI)")
    result = await submit_train_request(
        dapr,
        settings,
        model=body.model,
        features=[f.model_dump() for f in body.features],
        config=body.config,
        token=idempotency_key,
    )
    if result.get("status") == "resolve_failed":
        return _problem(422, "ValidationError", f"cannot resolve feature dataset {result['dataset']!r}")
    if result.get("status") == "publish_failed":
        return _problem(503, "ServiceUnavailable", "training trigger publish failed; retry")
    return result


def register_train_trigger_route(app: FastAPI, dapr_app: DaprApp | None = None) -> DaprApp:
    """Register the training-trigger subscription on ``app`` (reusing the producer's ``DaprApp``)."""
    settings = get_settings()
    dapr_app = dapr_app or DaprApp(app)

    @dapr_app.subscribe(
        pubsub=settings.pubsub,
        topic=settings.train_topic,
        route="/train-trigger",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_train_trigger(
        event: dict[str, Any],
        request: Request,
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
    ) -> dict[str, str]:
        """Thin wrapper over the testable :func:`handle_train_trigger` (submit-and-ack, D2).
        Authenticated by the Dapr app-api-token so a forged trigger can't spend training compute; the
        FGA client is the host app's (``app.state.fga`` — built by the producer lifespan when
        MEDALLION_FGA_ENABLED, ``None`` otherwise → gate off, symmetric with the movers)."""
        fga_client = getattr(request.app.state, "fga", None)
        return await handle_train_trigger(config, event, fga_client=fga_client)

    return dapr_app
