"""The Ray TRAIN head + trainer consumer business logic (#115a, docs/RAY-TRAIN.md D1/D2/D5).

``submit_train_request`` (the ``POST /train`` head) resolves every feature's Lance version — an omitted
version pins to LATEST **at the head** (D1: the pin is resolved here and threaded through, never left
floating inside the job) — then publishes the training trigger to the DEDICATED topic. The trigger
carries pointers only (dataset names + versions + a small config), never data (claim-check).

``handle_train_trigger`` (the subscription consumer) FGA-gates as the trainer's OWN identity —
``can_read_data`` on EVERY pinned input and ``can_create_table`` on the models namespace (D5: never the
medallion writer rung) — then SUBMITS the Ray job and acks (D2: submit-and-ack; the job emits its own
OpenLineage lifecycle). Deny → DROP before any compute is spent; FGA outage → RETRY; a terminally
FAILED prior job → DROP (no auto-resubmit of expensive training).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from common import dapr_publish, fga
from dapr.aio.clients import DaprClient
from fastapi.concurrency import run_in_threadpool
from lance_namespace import ServiceUnavailableError

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit


log = logging.getLogger(__name__)

_SUCCESS = {"status": "SUCCESS"}
_RETRY = {"status": "RETRY"}
_DROP = {"status": "DROP"}

#: Claim-check ceiling for the trigger's config payload (pointers + hyperparams, never data).
_MAX_CONFIG_BYTES = 8192
#: Ack-window bound on the per-input authz fan-out: the inputs gate is ONE batch_check round trip
#: regardless of count, but each feature is still a Lance read at the job — keep triggers sane.
#: Public: the /train route caps its request model with the same number (422 at the head, DROP here).
MAX_FEATURES = 16

#: One path-safe name segment. Trigger names (model, token, dataset stage/name) become S3 key
#: prefixes, Lance dataset URIs, and Ray env values — the bus is a wider trust surface than the
#: token-guarded head, so anything outside this shape (traversal dots, separators, whitespace) is
#: DROPped at the consumer, never repaired. The head validates the SAME shapes up front (public
#: pattern constants → pydantic → 422), so a doomed request is refused, not 202'd-then-DROPped.
MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
DATASET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\$[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_SAFE_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


def _safe_name(value: Any) -> bool:
    return isinstance(value, str) and _SAFE_SEGMENT.fullmatch(value) is not None


def _safe_dataset(value: Any) -> bool:
    """EXACTLY ``stage$name`` built from path-safe segments. A bare name is rejected on purpose:
    training features are stage tables (``silver$features``), and a bare name would both derive a
    wrong stage URI and — in the job's lineage — a namespace equal to the whole dataset name, which
    the ingest's ``SET d.namespace`` would then write onto the SHARED graph node (review 2026-07-11)."""
    if not isinstance(value, str):
        return False
    parts = value.split("$")
    return len(parts) == 2 and all(_SAFE_SEGMENT.fullmatch(part) for part in parts)


def train_head_enabled(settings: MedallionSettings) -> bool:
    """The train head needs the Ray path + S3 + a raw URI to derive stage URIs from — 409 otherwise
    (an explicit contract, like the media head; never a KeyError 500)."""
    return bool(settings.ray_enabled and settings.s3_endpoint and settings.raw_uri)


def _stage_base(settings: MedallionSettings) -> str:
    """The medallion (project) bucket base ``…/medallion`` where the STAGE datasets (bronze/silver) and the
    model registry live. Prefers the explicit ``MEDALLION_STAGE_BASE_URI`` — which stays correct when raw
    (ingest source) and gold (sink) are zoned into their OWN buckets — and falls back to the raw URI's
    parent for the single-bucket default (unchanged)."""
    if settings.stage_base_uri:
        return settings.stage_base_uri.rstrip("/")
    return settings.raw_uri.rstrip("/").rsplit("/", 1)[0]


def stage_uri_for(settings: MedallionSettings, dataset: str) -> str:
    """Derive a medallion dataset's URI from its name — ``silver$features`` → ``…/medallion/silver``.

    The cascade lays every stage out as a sibling under the medallion base (``…/medallion/<stage>``), so the
    stage URI is that base + the dataset's namespace segment. Demo-tier convention — a catalog-registered
    feature table would resolve through describe instead (future #115 work).
    """
    stage = dataset.split("$", 1)[0]
    return f"{_stage_base(settings)}/{stage}"


def registry_uri_for(settings: MedallionSettings, model: str) -> str:
    """The model-REGISTRY Lance dataset URI (D4 step 2): ``…/medallion/models/<model>`` — one dataset
    per model under the medallion base, so the registry sits in the project bucket beside the stages it
    trained on (NOT the raw source or gold sink bucket when those are zoned out)."""
    return f"{_stage_base(settings)}/models/{model}"


def artifact_base_for(settings: MedallionSettings, model: str) -> str:
    """The plain-path artifact base (D4 step 1): ``s3://<bucket>/models/<model>`` — at the BUCKET root,
    a separate tree from the registry dataset directory, so pointer targets are never inside any Lance
    dataset (GC must not see them as orphans; the #92 allowlist governs the ``models/`` prefix)."""
    base = _stage_base(settings)  # …/medallion in the PROJECT bucket
    if base.startswith("s3://"):
        bucket = "/".join(base.split("/", 3)[:3])  # s3://<bucket>
        return f"{bucket}/models/{model}"
    # local tier (unit tests): a sibling of the medallion layout, still outside the registry dataset
    # (`base` is already the medallion parent, so no further rsplit — keeps the pre-zone layout).
    return f"{base}/model-artifacts/{model}"


def _resolve_version(settings: MedallionSettings, dataset: str) -> int:
    """The dataset's CURRENT Lance version (blocking read — call via threadpool)."""
    import lance

    ds = lance.dataset(stage_uri_for(settings, dataset), storage_options=settings.storage_options())
    return int(ds.version)


async def submit_train_request(
    dapr: DaprClient,
    settings: MedallionSettings,
    *,
    model: str,
    features: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """Resolve feature-version pins and publish the training trigger; returns ``{token, features}``.

    A resolution failure (unknown dataset / unreadable storage) surfaces as ``resolve_failed`` and a
    publish failure as ``publish_failed`` — the route maps both to explicit errors rather than a 202
    that silently trains against nothing.
    """
    # Idempotency: a caller-supplied key (its 503-retry contract) REUSES the token, so deterministic
    # run_ids MERGE the duplicate instead of double-firing an unrelated training run (bug hunt 2026-07-13).
    token = token or uuid.uuid4().hex[:12]
    # Claim-check guard: the trigger carries pointers + a SMALL config — never data-shaped content
    # (NATS messages must stay small JSON; an inlined matrix would degrade the broker for everyone).
    if len(json.dumps(config or {})) > _MAX_CONFIG_BYTES:
        return {"status": "config_too_large"}
    pinned: list[dict[str, Any]] = []
    for feature in features:
        dataset = feature["dataset"]
        version = feature.get("version")
        if version is None:
            try:
                version = await run_in_threadpool(_resolve_version, settings, dataset)
            except Exception as exc:
                log.warning("train_resolve_failed", extra={"dataset": dataset, "error": str(exc)})
                return {"status": "resolve_failed", "dataset": dataset}
        pinned.append({"dataset": dataset, "version": int(version)})
    payload = {"token": token, "model": model, "features": pinned, "config": config or {}}
    try:
        await dapr_publish.publish_event(
            dapr,
            timeout_seconds=settings.publish_timeout_seconds,
            pubsub_name=settings.pubsub,
            topic_name=settings.train_topic,
            data=json.dumps(payload),
            data_content_type="application/json",
        )
    except Exception as exc:
        log.warning("train_publish_failed", extra={"token": token, "error": str(exc)})
        return {"status": "publish_failed", "token": token}
    log.info("train_requested", extra={"token": token, "model": model, "features": pinned})
    return {"token": token, "model": model, "features": pinned}


async def handle_train_trigger(settings: MedallionSettings, event: Any, *, fga_client: Any | None = None) -> dict[str, str]:
    """Consume one training trigger: gate, submit-and-ack (D2) — never block on job completion.

    ``event`` is the untrusted Dapr CloudEvent envelope. Malformed → DROP (redelivery can't fix a bad
    payload). With FGA on: ``can_read_data`` on every input + ``can_create_table`` on the models
    namespace as the TRAINER identity; deny → DROP (attributable — redelivery won't grant the rung),
    outage → RETRY. A transport/submit failure → RETRY (the deterministic id re-attaches); a prior
    terminally-FAILED job → DROP (D2: no auto-resubmit of expensive training).
    """
    data = event.get("data") if isinstance(event, dict) else None
    if not isinstance(data, dict) or not _safe_name(data.get("token")) or not _safe_name(data.get("model")):
        log.warning("train_trigger_malformed", extra={"event": str(event)[:200]})
        return _DROP
    token, model = data["token"], data["model"]
    # STRICT feature validation (review 2026-07-10): every entry needs a PATH-SAFE dataset AND an int
    # version — a version-less feature would train on floating LATEST (violates D1 + #115b's guardrail),
    # an empty/malformed list would pass the per-input gate VACUOUSLY, and an unsafe name would flow
    # into S3 key derivation (#115b: names → URIs here). Either way: DROP, not repair.
    raw_features = data.get("features") or []
    features = [f for f in raw_features if isinstance(f, dict) and _safe_dataset(f.get("dataset")) and isinstance(f.get("version"), int)]
    if not features or len(features) != len(raw_features) or len(features) > MAX_FEATURES:
        log.warning("train_trigger_malformed", extra={"token": token, "features": str(raw_features)[:200]})
        return _DROP
    # Re-apply the head's claim-check bound HERE too (review 2026-07-11): the bus is a wider trust
    # surface than the token-guarded head, and this config flows verbatim into the Ray Jobs API
    # runtime_env — a forged non-dict or oversized config is a malformed trigger, DROP.
    config = data.get("config") or {}
    if not isinstance(config, dict) or len(json.dumps(config)) > _MAX_CONFIG_BYTES:
        log.warning("train_trigger_malformed", extra={"token": token, "config": str(config)[:200]})
        return _DROP

    if fga_client is not None:
        try:
            # ONE batch round trip for all inputs (review 2026-07-11): N sequential checks, each with
            # its own ~15s retry budget, could push a many-feature trigger past the 30s ack window.
            allowed = await fga.batch_check(
                fga_client,
                user=settings.trainer_identity,
                relation="can_read_data",
                objects=[f"table:{feature['dataset']}" for feature in features],
            )
            denied = [obj for obj, ok in allowed.items() if not ok]
            if denied:
                log.warning("train_denied", extra={"token": token, "input": denied[0]})
                return _DROP
            if not await fga.check(
                fga_client,
                user=settings.trainer_identity,
                relation="can_create_table",
                obj=f"namespace:{settings.models_namespace}",
            ):
                log.warning("train_denied", extra={"token": token, "input": "models namespace"})
                return _DROP
        except ServiceUnavailableError as exc:
            log.warning("train_fga_unavailable", extra={"token": token, "error": str(exc)})
            return _RETRY
        # The per-model parent link (#115c: `namespace:models parent table:models$<m>`) — seeded
        # HERE, before the submit, exactly like the seed script pre-links the mover datasets: the
        # movers write Lance directly, so without this tuple no human rung ever cascades to the
        # registry dataset and the published model is INVISIBLE in /runs, /datasets/*, /graph under
        # LINEAGE_FGA_ENABLED. Idempotent (duplicate writes are swallowed); a dangling link for a
        # job that later fails is harmless (same posture as the pre-seeded mover links). Placed
        # before the ack so an outage RETRYs rather than acking with the link half-missing.
        from common.fga import ClientTuple  # openfga_sdk re-export — only needed on this path

        try:
            await fga.write_tuples(
                fga_client,
                [
                    ClientTuple(
                        user=f"namespace:{settings.models_namespace}",
                        relation="parent",
                        object=f"table:{settings.models_namespace}${model}",
                    )
                ],
            )
        except ServiceUnavailableError as exc:
            log.warning("train_parent_link_unavailable", extra={"token": token, "error": str(exc)})
            return _RETRY

    # Enrich each validated feature with its Lance URI and derive the D4 publish pointers HERE — the
    # job is deliberately dumb about layout (it reads FEATURES[].uri and writes to REGISTRY_URI /
    # ARTIFACT_BASE verbatim), so the storage-layout convention lives in exactly one service. Built
    # from the VALIDATED fields only — never `{**f, …}` — so unvalidated extra keys on a bus-forged
    # feature dict can't ride into the Ray job env.
    enriched = [{"dataset": f["dataset"], "version": f["version"], "uri": stage_uri_for(settings, f["dataset"])} for f in features]
    try:
        outcome = await ray_submit.submit_train_job(
            settings,
            model=model,
            features_json=json.dumps(enriched),
            config_json=json.dumps(config),
            token=token,
            registry_uri=registry_uri_for(settings, model),
            artifact_base=artifact_base_for(settings, model),
        )
    except ray_submit.RayJobError as exc:
        log.warning("train_submit_failed", extra={"token": token, "error": str(exc)})
        return _RETRY
    if outcome == "already_failed":
        log.warning("train_previously_failed", extra={"token": token, "model": model})
        return _DROP
    log.info("train_job_dispatched", extra={"token": token, "model": model, "outcome": outcome})
    return _SUCCESS
