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

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from service_kit.lancekit.keys import validate_doc_key
from service_kit.media.deps import DatasetParam, StateDep
from service_kit.media.exceptions import ServiceUnavailableError
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


class AssistShape(BaseModel):
    """One predicted shape in image coordinates (box or polygon) with confidence."""

    shape_type: str = "rectangle"
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


@router.post("/assist/{doc_id}/{speech_id}/{chunk_id}")
def assist(
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
    url = state.settings.assist_url
    shapes = _remote(state, url, (doc_id, speech_id, chunk_id), body) if url else _mock(body)
    # Prompt CONTENT is user free-text — never logged (PII/leak surface); length only.
    logger.info("assist %s (prompt %d chars) → %d shape(s)", body.producer, len(body.prompt or ""), len(shapes))
    return AssistResult(shapes=shapes, source=source)


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
