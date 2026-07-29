"""#17/#42 model-registry endpoints: registry listing, candidate→blessed promotion, and a describe read.

The model registry is a Lance dataset per model at ``settings.model_uri(model)`` (the medallion trainer
writes it there directly). These routes ARE under the router-level ``authorize`` dependency — its pre-path
checks still run here (missing token → 401; FGA enabled but unwired → 503, fail-closed) — but ``/v1/model``
matches no ``_RESOURCES`` prefix, so ``authorize`` makes no per-object decision. The per-object FGA gate is
therefore explicit per handler: promote → the validator rung ``can_promote``; describe → the reader rung
``can_get_metadata``; list → the same reader rung as a ``list_objects`` filter — all on
``table:models$<model>`` (the per-model FGA object the trainer seeds at train time, so the rung cascades
from ``namespace:models``). That router-level fail-closed floor is what keeps the list filter's
``fga_enabled and token and client`` guards from ever being a fail-open path. Promotion moves the
``blessed`` Lance tag after a fail-closed metrics gate; a distinct promotion RunEvent is emitted
best-effort so the blessing lands in lineage without being mistaken for a training run.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import InvalidInputError, TableNotFoundError

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, LineageEmitterDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.lineage_emit import PROMOTE_MODEL, emit_write_event
from catalog.schemas import (
    ModelArtifact,
    ModelDescribeResponse,
    ModelsListResponse,
    ModelSummary,
    PromoteRequest,
    PromoteResponse,
)
from catalog.services import models as registry
from service_kit.governed import fga


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/model", tags=["model"])

# The FGA namespace the per-model object lives under (``table:<ns>$<model>``) — matches the medallion
# trainer's MEDALLION_MODELS_NAMESPACE default + scripts/seed_medallion_fga.sh. A model name is a
# table-name-shaped fragment (kept strict so it can't traverse the registry path or forge an FGA object id).
# fullmatch (not match) so a trailing newline can't sneak past the ``$`` anchor into an S3 key.
_MODELS_NAMESPACE = "models"
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Promotion moves a KNOWN lifecycle tag, never an arbitrary caller-named ref — this endpoint is "promote",
# not "create any tag" (the owner-tier /v1/table/.../tags/* routes are the general tag API). blessed = the
# served pointer; staging/prod are the promotion ladder (RAY-TRAIN D4.3).
_PROMOTION_TAGS = frozenset({registry.BLESSED_TAG, "staging", "prod"})


def _segments(model: str) -> list[str]:
    if not _MODEL_RE.fullmatch(model):
        raise InvalidInputError(f"invalid model name: {model!r}")
    return [_MODELS_NAMESPACE, model]


def _summaries(settings: Settings, so: dict[str, str], names: list[str]) -> list[ModelSummary]:
    """Per-model list summaries, one blocking pass (callers threadpool the whole loop). A directory that is
    not a readable Lance dataset yet (interrupted first publish) reports null versions instead of failing
    or hiding the whole listing."""
    out: list[ModelSummary] = []
    for name in names:
        try:
            summary = registry.summarize(settings.model_uri(name), so)
        except TableNotFoundError:
            out.append(ModelSummary(model=name, latest_version=None, blessed_version=None))
            continue
        out.append(ModelSummary(model=name, **summary))
    return out


@router.get("")
async def list_models(
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> ModelsListResponse:
    """List the model registry: every model with its candidate (latest) + blessed versions.

    Mirrors the governed table listing: enumerate storage, then — when FGA is on — return only the models
    the caller holds the reader rung (``can_get_metadata``) on, the same relation the per-model describe
    enforces. Name-shape filtering runs before authz so a stray directory can never reach an FGA object id.
    Metrics are deliberately absent here (see :func:`catalog.services.models.summarize`) — the per-model
    describe carries them. ``limit`` bounds the per-request dataset opens (one per listed model);
    truncation is deterministic (name-sorted first ``limit``).
    """
    names = await run_in_threadpool(registry.list_models, settings.models_root, so)
    names = [name for name in names if _MODEL_RE.fullmatch(name)]
    if settings.fga_enabled and token is not None and client is not None:
        allowed = set(await fga.list_objects(client, user=token.sub, relation="can_get_metadata", object_type="table"))
        names = [name for name in names if f"table:{fga.canonical_object_id(_segments(name), delimiter=settings.delimiter)}" in allowed]
    return ModelsListResponse(models=await run_in_threadpool(_summaries, settings, so, names[:limit]))


@router.post("/{model}/promote", response_model_exclude_none=True)
async def promote_model(
    model: str,
    body: PromoteRequest,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> PromoteResponse:
    """Move the ``blessed`` tag to model ``version`` — validator-gated, metrics-gated, lineage-emitting.

    Authorization is the VALIDATOR rung (``can_promote``), a deliberately higher bar than a plain writer:
    training PUBLISHES candidates (writer), a validator BLESSES one (the model analog of silver→gold). The
    metrics gate runs inside the fail-closed promote op BEFORE the irreversible tag move; the promotion
    RunEvent is best-effort AFTER (a lineage outage never fails a promotion).
    """
    segments = _segments(model)
    if body.tag not in _PROMOTION_TAGS:
        raise InvalidInputError(f"tag must be one of {sorted(_PROMOTION_TAGS)}, got {body.tag!r}")
    await fga_deps.require_can_promote(client, settings, token, segments=segments)
    blessed = await run_in_threadpool(
        registry.promote,
        settings.model_uri(model),
        so,
        version=body.version,
        min_metrics=body.min_metrics or None,
        tag=body.tag,
    )
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=blessed,
        operation=PROMOTE_MODEL,
        authorization=authorization,
        source_uri=settings.model_uri(model),
    )
    log.info("model_promoted", extra={"model": model, "version": blessed, "tag": body.tag})
    return PromoteResponse(model=model, blessed_version=blessed, tag=body.tag)


@router.get("/{model}", response_model_exclude_none=True)
async def describe_model(
    model: str,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> ModelDescribeResponse:
    """Registry summary: the latest (candidate) version, the blessed version (or null), both metrics, and
    the model's plain-path artifact objects (path/size/mtime under ``models/<model>/<token>/…`` — the
    #17/#92 layout; ``[]`` when none are laid out).

    Reader-gated (``can_get_metadata``) — a reader may see which version is blessed and compare metrics; only
    a validator may move the pointer.
    """
    segments = _segments(model)
    await fga_deps.require_can_get_metadata(client, settings, token, segments=segments)
    summary = await run_in_threadpool(registry.describe, settings.model_uri(model), so)
    artifacts = await run_in_threadpool(registry.list_artifacts, settings.model_artifacts_uri(model), so)
    return ModelDescribeResponse(model=model, artifacts=[ModelArtifact(**a) for a in artifacts], **summary)
