"""Interactive AI-assist — a model prediction for the annotator (prompt/draw → shapes).

The NARROW interactive exception (bulk auto-labeling belongs in batch derivers). A
prompt — text for GroundingDINO, a box/point for SAM — runs a model and returns shapes
as `status="prediction"`, `source="model:<name>"` rows the annotator renders optimistically
and the reviewer accepts/rejects like any prediction. So interactive assist and batch
auto-label share the SAME provenance + review path.

Routes to a model server (``MEDIA_ASSIST_URL``) when set; else a deterministic MOCK so
the round-trip is wired + testable in-repo (drop-in for a real server, exactly like the
catalog transport). Shapes are in IMAGE coordinates — the annotator's own space.
"""

import logging
from typing import Any

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from service_kit.exceptions import ServiceUnavailableError
from service_kit.lancekit.keys import validate_doc_key
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.state import AppState, dataset_handle


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["assist"])


class Region(BaseModel):
    """The drawn box the assist runs within (image coords). Omitted ⇒ whole image."""

    x: float
    y: float
    width: float
    height: float


class AssistRequest(BaseModel):
    """What to run: the producer + its prompt (free text and/or a drawn region)."""

    producer: str = "grounding-dino"
    prompt: str | None = None
    region: Region | None = None
    #: The labeling task this assist is for, when there is one. The SERVER reads that task's
    #: captured template — the client does not send the rules it is judged by, same posture as
    #: `review_required` and the submit-time template check.
    task_id: str | None = None


#: Producer tool-name -> the canonical vocabulary the draft, ontology and published table speak.
#: A model server names shapes however it likes; `"rectangle"` was this endpoint's own default and
#: is accepted by NEITHER the service (`bbox`) nor the canvas. Normalizing here means a new backend
#: is a config entry rather than a new dialect leaking into the annotations table.
_CANONICAL_SHAPE: dict[str, str] = {
    "rectangle": "bbox",
    "rect": "bbox",
    "box": "bbox",
    "bbox": "bbox",
    "polygon": "polygon",
    "mask": "mask",
    "point": "keypoint",
    "keypoint": "keypoint",
    "line": "polyline",
    "polyline": "polyline",
    "baseline": "polyline",
}


#: What each known producer FAMILY emits, longest-prefix like the backend registry itself.
#: Absent ⇒ unknown, and every surface must say "unknown" rather than guess — a wrong claim here
#: would present a producer as task-compatible when it is not, which is the failure this whole
#: endpoint exists to prevent.
_RETURNS: dict[str, tuple[str, ...]] = {
    "grounding-dino": ("bbox",),
    "sam": ("polygon",),
}


def returns_for(producer: str) -> tuple[str, ...]:
    """The shape types `producer` is known to emit; empty tuple when nothing is known."""
    best = max((p for p in _RETURNS if producer.startswith(p)), key=len, default=None)
    return _RETURNS[best] if best is not None else ()


class ProducerInfo(BaseModel):
    """One row of the assist registry, as the SERVICE sees it.

    The zone used to derive this list by parsing `MEDIA_ASSIST_BACKENDS` out of the WEB pod's own
    env — a second copy of the registry that the config comment itself admitted could "drift from
    the service's". The service is the process that actually resolves and calls a backend, so it is
    the only source that cannot be wrong.
    """

    name: str
    #: False ⇒ this name answers from the in-repo mock (no endpoint resolves for it).
    configured: bool
    #: The shape types it emits; EMPTY means unknown, not "none".
    returns: list[str] = Field(default_factory=list)
    #: Whether those shapes satisfy the task named in `?task_id=`. None when there is no task, when
    #: the task does not enforce, or when `returns` is unknown — three genuinely different reasons
    #: not to make a claim, all of which the UI renders as "unknown" rather than as a pass.
    compatible: bool | None = None


class ProducerListing(BaseModel):
    """The registry plus the fallback, so a surface can explain WHY a producer is mocked."""

    producers: list[ProducerInfo]
    #: True when `MEDIA_ASSIST_URL` is set: unregistered producer names reach a real backend.
    default_configured: bool
    #: Endpoints are never returned. Any authenticated annotator can call this, and an internal
    #: model-server URL is not theirs to have; presence is the whole of what a surface needs.
    urls_redacted: bool = True


class AssistShape(BaseModel):
    """One predicted shape in image coordinates (box or polygon) with confidence."""

    shape_type: str = "bbox"
    x: float
    y: float
    width: float
    height: float
    polygon: list[float] = Field(default_factory=list)
    label: str = ""
    confidence: float = 0.0


class AssistResult(BaseModel):
    """Predicted shapes + the provenance stamp (source) the annotator writes."""

    shapes: list[AssistShape]
    source: str
    #: Predictions the TASK's own contract refuses, and why — reported rather than silently dropped.
    #:
    #: A producer that returns a polygon for a bbox-only task is a real mismatch, and the annotator
    #: is the wrong place to discover it: before this, such a shape landed in the review queue and
    #: was refused only at SUBMIT, after a human had already looked at it. Dropping it silently
    #: would be worse — the operator would never learn the backend disagrees with the task.
    dropped: list[str] = Field(default_factory=list)


@router.get("/assist/producers")
async def producers(state: StateDep, task_id: str | None = None) -> ProducerListing:
    """Which producers exist, whether each is real or mocked, and what it returns.

    The owner's question, verbatim: "why is there no settings menu for which endpoints or runner is
    configured.. and what they return". There was none, and worse, the one list that DID exist was
    the zone re-parsing the registry out of its own pod's env — so a service configured with a real
    backend could still be presented as mocked, and vice versa.

    With `?task_id=`, each row also carries whether its output can satisfy that task. That is the
    other half of "schemas must align from the ml backend": a producer emitting polygons for a
    bbox-only task is answerable BEFORE anyone runs it, and this is where you can see it.
    """
    return producer_listing(state.settings, await enforced_shape_types(task_id))


async def enforced_shape_types(task_id: str | None) -> set[str] | None:
    """The shape types a task will ACCEPT, canonicalised. `None` = no constraint to speak of.

    The three ways to get `None` are deliberately indistinguishable to callers — no task, an
    unenforced template, or a task that could not be read. All three mean the same thing: there is
    no rule here that anyone may be judged against, so make no claim.
    """
    if not task_id:
        return None
    template = await _task_template(task_id)
    if not template.get("enforce"):
        return None
    return {_CANONICAL_SHAPE.get(t, t) for t in (template.get("tools") or [])} or None


def producer_listing(settings: Any, allowed: set[str] | None = None) -> ProducerListing:
    """Build the registry report. Takes `settings` structurally, like `backend_for` — the routing
    rules are the interesting part and they should be testable without standing up an `AppState`."""
    registry: dict[str, str] = getattr(settings, "assist_backends", None) or {}
    default_url = getattr(settings, "assist_url", None)

    rows: list[ProducerInfo] = []
    # The registry, plus the two families the in-repo mock answers for — otherwise an unconfigured
    # service reports an EMPTY settings surface, which reads as "assist is unavailable" when in fact
    # both interactive loops work against the mock.
    for name in sorted(set(registry) | set(_RETURNS)):
        emits = returns_for(name)
        rows.append(
            ProducerInfo(
                name=name,
                configured=backend_for(settings, name) is not None,
                returns=list(emits),
                # No task, no enforcement, or nothing known about what it emits ⇒ NO CLAIM. Only the
                # case where both sides are actually known produces a true/false.
                compatible=bool(set(emits) & allowed) if (allowed and emits) else None,
            )
        )
    return ProducerListing(producers=rows, default_configured=bool(default_url))


@router.post("/assist/{doc_id}/{speech_id}/{chunk_id}")
async def assist(
    state: StateDep,
    doc_id: str,
    speech_id: int,
    chunk_id: int,
    body: AssistRequest,
    dataset: DatasetParam = None,
) -> AssistResult:
    """Run an interactive producer over one media unit and return predicted shapes."""
    handle = dataset_handle(state, dataset)
    doc_id = validate_doc_key(handle.descriptor.declared, doc_id)
    source = f"model:{body.producer}"
    url = backend_for(state.settings, body.producer)
    # The producer call uses a SYNC httpx client, so it rides the threadpool rather than blocking
    # the event loop — the endpoint is async only because reading the task's template is.
    shapes = await run_in_threadpool(_remote, state, url, (doc_id, speech_id, chunk_id), body) if url else _mock(body)
    for shape in shapes:
        shape.shape_type = _CANONICAL_SHAPE.get(shape.shape_type, shape.shape_type)
    shapes, dropped = await _within_contract(shapes, body.task_id)
    # Prompt CONTENT is user free-text — never logged (PII/leak surface); length only.
    logger.info(
        "assist %s (prompt %d chars) → %d shape(s), %d dropped",
        body.producer,
        len(body.prompt or ""),
        len(shapes),
        len(dropped),
    )
    return AssistResult(shapes=shapes, source=source, dropped=dropped)


async def _within_contract(shapes: list[AssistShape], task_id: str | None) -> tuple[list[AssistShape], list[str]]:
    """Keep the predictions the task's template permits; report the rest.

    A producer is not obliged to know the task's rules — the whole point of the registry is that a
    backend is a config entry. So the mismatch is resolved HERE, once, rather than by every model
    server or (as before) by a human at submit time.

    A task-less assist (the ad-hoc canvas) has no contract, so nothing is dropped. A read that fails
    is treated the same way: the assist still returns its shapes, because refusing a prediction
    because we could not read a rule would be a worse failure than the mismatch it guards against.
    """
    if not task_id:
        return shapes, []
    template = await _task_template(task_id)
    if not template.get("enforce"):
        return shapes, []
    allowed = set(template.get("tools") or [])
    if not allowed:
        return shapes, []
    kept, dropped = [], []
    for shape in shapes:
        if shape.shape_type in allowed:
            kept.append(shape)
        else:
            dropped.append(f"{shape.shape_type} refused — task {template.get('kind') or 'template'} allows {sorted(allowed)}")
    return kept, dropped


async def _task_template(task_id: str) -> dict[str, Any]:
    """The template CAPTURED onto a task, read server-side. `{}` when it cannot be read.

    Read here rather than accepted from the caller on purpose: the client would otherwise be
    supplying the rules it is judged by. An empty dict means "no claim" everywhere it is used —
    nothing filters, nothing is reported compatible — so a transport failure degrades to the
    unconstrained behaviour rather than to a wrong answer.
    """
    try:
        from annotator.api.v1.endpoints.tasks import _proxy  # noqa: PLC0415 - import cycle

        task = await _proxy(task_id).get()
    except Exception:  # noqa: BLE001 - an unreadable rule must not lose a prediction; see callers
        logger.warning("assist could not read task %s; proceeding without its contract", task_id)
        return {}
    return (task or {}).get("template") or {}


def backend_for(settings: Any, producer: str) -> str | None:
    """Resolve the producer's backend: LONGEST matching prefix in the registry wins (so `"sam"`
    covers `sam-click` while `"sam-hq"` can still override it), else the default `assist_url`,
    else None (→ the honest in-repo mock). The registry is what makes a new model a CONFIG entry
    rather than a code change — the CVAT-functions shape without the function orchestrator."""
    backends = getattr(settings, "assist_backends", None) or {}
    best = max((p for p in backends if producer.startswith(p)), key=len, default=None)
    if best is not None:
        return backends[best]
    return settings.assist_url


_SAM_CLICK_PATCH = 120.0  # default box side for a bare click (a zero-size region)


def _mock(body: AssistRequest) -> list[AssistShape]:
    """Deterministic stand-in for a model server, so both interactive loops round-trip
    in-repo. GroundingDINO → a box at the drawn region (or a default), labeled with the
    prompt. SAM → a polygon 'mask' around the region/click (a click is a zero box, so a
    default patch is grown around the point). Both render + review like real predictions."""
    r = body.region
    if body.producer.startswith("sam"):
        x, y, w, h = _region_box(r)
        return [
            AssistShape(
                shape_type="polygon",
                x=x,
                y=y,
                width=w,
                height=h,
                polygon=_diamond(x, y, w, h),
                label=(body.prompt or "object").strip(),
                confidence=0.85,
            )
        ]
    label = (body.prompt or "region").strip()
    if r is not None:
        return [
            AssistShape(
                shape_type="rectangle",
                x=r.x,
                y=r.y,
                width=r.width,
                height=r.height,
                label=label,
                confidence=0.7,
            )
        ]
    return [AssistShape(shape_type="rectangle", x=100.0, y=100.0, width=200.0, height=80.0, label=label, confidence=0.7)]


def _region_box(r: Region | None) -> tuple[float, float, float, float]:
    """The region as (x, y, w, h); a click (near-zero size) becomes a patch centered on
    the point."""
    if r is None:
        return (100.0, 100.0, 200.0, 200.0)
    w = r.width if r.width > 1 else _SAM_CLICK_PATCH
    h = r.height if r.height > 1 else _SAM_CLICK_PATCH
    x = r.x if r.width > 1 else r.x - w / 2
    y = r.y if r.height > 1 else r.y - h / 2
    return (x, y, w, h)


def _diamond(x: float, y: float, w: float, h: float) -> list[float]:
    """A simple inset polygon (rhombus) standing in for a segmentation mask — flat
    [x0,y0,x1,y1,...] in image coords, as the engine's ArrowDataPlugin expects."""
    cx, cy = x + w / 2, y + h / 2
    return [cx, y, x + w, cy, cx, y + h, x, cy]


def _remote(state: AppState, url: str, key: tuple[str, int, int], body: AssistRequest) -> list[AssistShape]:
    """Proxy to the model endpoint — a Ray Serve deployment (GroundingDINO/SAM) per the
    merge runtime stack. WIRED, not exercised in-repo: posts the chunk-frame image URL +
    prompt + region and expects ``{shapes: [...]}``. A failing or misbehaving model
    server (HTTP error, bad JSON, invalid shape) raises
    :class:`ServiceUnavailableError` — a stable 503, never a raw 500."""
    doc_id, speech_id, chunk_id = key
    http = state.http
    if http is None:
        raise ServiceUnavailableError("assist: HTTP client unavailable")
    payload = {
        "image_url": f"/api/chunk-frame/{doc_id}/{speech_id}/{chunk_id}",
        "prompt": body.prompt,
        "region": body.region.model_dump() if body.region else None,
    }
    try:
        resp = http.post(url, json=payload, timeout=30.0)
        resp.raise_for_status()
        return [AssistShape.model_validate(s) for s in resp.json().get("shapes", [])]
    except (httpx.HTTPError, ValueError) as e:
        raise ServiceUnavailableError("assist model server error") from e
