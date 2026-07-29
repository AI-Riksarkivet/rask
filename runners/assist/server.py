"""The assist model server — real GroundingDINO+SAM behind ``MEDIA_ASSIST_URL`` (CPU).

Implements the annotator's remote-assist contract (services/annotator .. assist.py::``_remote``):
one POST with ``{image_url, prompt, region}`` returns ``{shapes: [...]}`` in the exact
``AssistShape`` wire shape the annotator validates. The payload deliberately carries no
``producer`` field, so routing is inferred from what the user actually gave:

* ``prompt`` present  → **GroundingDINO-tiny** open-vocabulary detection (text-prompted);
  a drawn ``region`` narrows detection to that crop (boxes come back in full-image coords).
* ``region`` only     → **SAM-ViT-base** segmentation — a real box becomes a box prompt, a
  click (a ~zero-size region, the annotator's click convention) a point prompt; the best
  mask returns as a simplified polygon.

The frame itself is fetched from the viewer service (``ASSIST_FRAME_BASE`` + the payload's
relative ``image_url``); absolute URLs are rejected so this server can only ever read from
its configured in-cluster peer (no SSRF surface). Weights are baked into the image at build
(``HF_HUB_OFFLINE=1`` at runtime) — model load happens once in the lifespan, and ``/readyz``
turns 200 only after both models are up. Inference is serialized behind a lock: one frame at
a time keeps the memory ceiling flat (interactive use is single-user bursts, not throughput).
"""

import io
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import cv2
import httpx
import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)

#: A region whose width/height are at or below this is a CLICK (the annotator sends a
#: zero-size region for a bare click) → SAM gets a point prompt instead of a box.
CLICK_EPSILON = 1.0

#: Polygon simplification tolerance as a fraction of the contour perimeter.
POLY_EPSILON_FRAC = 0.01


class Settings(BaseSettings):
    """Env-driven config (``ASSIST_`` prefix), mirroring the media services' MEDIA_ style."""

    model_config = SettingsConfigDict(env_prefix="ASSIST_")

    host: str = "127.0.0.1"
    port: int = 8110
    # The viewer service — the only host frames are fetched from (relative image_url only).
    frame_base: str = "http://localhost:8101"
    detector_model: str = "IDEA-Research/grounding-dino-tiny"
    segmenter_model: str = "facebook/sam-vit-base"
    box_threshold: float = 0.3
    text_threshold: float = 0.25


class Region(BaseModel):
    """The drawn box (image coords) — the annotator's ``Region`` wire shape."""

    x: float
    y: float
    width: float
    height: float

    def is_click(self) -> bool:
        return self.width <= CLICK_EPSILON or self.height <= CLICK_EPSILON


class AssistPayload(BaseModel):
    """What the annotator's ``_remote`` posts: the frame URL + the user's prompt/region."""

    image_url: str
    prompt: str | None = None
    region: Region | None = None


class Shape(BaseModel):
    """One predicted shape — field-for-field the annotator's ``AssistShape``."""

    shape_type: str = "rectangle"
    x: float
    y: float
    width: float
    height: float
    polygon: list[float] = Field(default_factory=list)
    label: str = ""
    confidence: float = 0.0


class AssistResponse(BaseModel):
    """``{shapes: [...]}`` — all the caller reads; ``model`` is provenance for curl/debug."""

    shapes: list[Shape]
    model: str


class Models:
    """Both loaded model pairs + the inference lock (constructed once in the lifespan)."""

    def __init__(self, settings: Settings) -> None:
        from transformers import (
            AutoModelForZeroShotObjectDetection,
            AutoProcessor,
            SamModel,
            SamProcessor,
        )

        self.settings = settings
        self.lock = threading.Lock()
        logger.info("loading detector %s", settings.detector_model)
        self.det_processor = AutoProcessor.from_pretrained(settings.detector_model)
        self.detector = AutoModelForZeroShotObjectDetection.from_pretrained(settings.detector_model)
        self.detector.eval()
        logger.info("loading segmenter %s", settings.segmenter_model)
        self.sam_processor = SamProcessor.from_pretrained(settings.segmenter_model)
        self.segmenter = SamModel.from_pretrained(settings.segmenter_model)
        self.segmenter.eval()
        logger.info("models ready")

    def detect(self, image: Image.Image, prompt: str, region: Region | None) -> list[Shape]:
        """Text-prompted detection; a region narrows to its crop, boxes map back to image coords."""
        crop, (ox, oy) = _crop(image, region)
        # GroundingDINO wants lower-cased, period-terminated phrases.
        text = prompt.strip().rstrip(".").lower() + "."
        with self.lock, torch.inference_mode():
            inputs = self.det_processor(images=crop, text=text, return_tensors="pt")
            outputs = self.detector(**inputs)
            result = self.det_processor.post_process_grounded_object_detection(
                outputs,
                inputs.input_ids,
                threshold=self.settings.box_threshold,
                text_threshold=self.settings.text_threshold,
                target_sizes=[crop.size[::-1]],
            )[0]
        shapes = []
        for score, box, label in zip(result["scores"], result["boxes"], result["text_labels"], strict=True):
            x0, y0, x1, y1 = (float(v) for v in box)
            shapes.append(
                Shape(
                    x=x0 + ox,
                    y=y0 + oy,
                    width=x1 - x0,
                    height=y1 - y0,
                    label=str(label) or prompt.strip(),
                    confidence=round(float(score), 4),
                )
            )
        return shapes

    def segment(self, image: Image.Image, region: Region, label: str) -> list[Shape]:
        """SAM over a box or click prompt → the best mask as a simplified polygon shape."""
        if region.is_click():
            prompt_kwargs: dict[str, Any] = {"input_points": [[[region.x, region.y]]]}
        else:
            box = [region.x, region.y, region.x + region.width, region.y + region.height]
            prompt_kwargs = {"input_boxes": [[box]]}
        with self.lock, torch.inference_mode():
            inputs = self.sam_processor(image, return_tensors="pt", **prompt_kwargs)
            outputs = self.segmenter(**inputs)
            masks = self.sam_processor.image_processor.post_process_masks(outputs.pred_masks.cpu(), inputs["original_sizes"], inputs["reshaped_input_sizes"])
        scores = outputs.iou_scores[0][0]
        best = int(scores.argmax())
        mask = masks[0][0][best].numpy().astype(np.uint8)
        polygon = _mask_to_polygon(mask)
        if not polygon:
            return []
        xs, ys = polygon[0::2], polygon[1::2]
        x0, y0 = min(xs), min(ys)
        return [
            Shape(
                shape_type="polygon",
                x=x0,
                y=y0,
                width=max(xs) - x0,
                height=max(ys) - y0,
                polygon=polygon,
                label=label,
                confidence=round(float(scores[best]), 4),
            )
        ]


def _crop(image: Image.Image, region: Region | None) -> tuple[Image.Image, tuple[float, float]]:
    """The region's crop (clamped to the frame) + its offset; the whole image when absent/degenerate."""
    if region is None or region.is_click():
        return image, (0.0, 0.0)
    x0 = max(0, int(region.x))
    y0 = max(0, int(region.y))
    x1 = min(image.width, int(region.x + region.width))
    y1 = min(image.height, int(region.y + region.height))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return image, (0.0, 0.0)
    return image.crop((x0, y0, x1, y1)), (float(x0), float(y0))


def _mask_to_polygon(mask: np.ndarray) -> list[float]:
    """The largest contour of a binary mask as a simplified flat [x0,y0,x1,y1,...] polygon."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return []
    contour = max(contours, key=cv2.contourArea)
    epsilon = POLY_EPSILON_FRAC * cv2.arcLength(contour, closed=True)
    approx = cv2.approxPolyDP(contour, epsilon, closed=True)
    if len(approx) < 3:
        return []
    return [float(v) for point in approx.reshape(-1, 2) for v in point]


def _fetch_frame(client: httpx.Client, image_url: str) -> Image.Image:
    """Fetch the frame from the viewer. Relative paths only — the base is the trust boundary."""
    if not image_url.startswith("/"):
        raise HTTPException(status_code=422, detail="image_url must be a relative path")
    try:
        resp = client.get(image_url, timeout=15.0)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"frame fetch failed: {e}") from e
    try:
        return Image.open(io.BytesIO(resp.content)).convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=422, detail="frame is not a decodable image") from e


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    app.state.settings = settings
    app.state.http = httpx.Client(base_url=settings.frame_base)
    app.state.models = Models(settings)
    try:
        yield
    finally:
        app.state.http.close()


app = FastAPI(title="assist-runner", lifespan=lifespan)


@app.get("/livez")
def livez() -> dict[str, bool]:
    """Process-up (the k8s liveness/startup signal — never touches the models)."""
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, bool]:
    """Ready only once both models are loaded (the lifespan finished)."""
    if getattr(app.state, "models", None) is None:
        raise HTTPException(status_code=503, detail="models not loaded")
    return {"ok": True}


@app.post("/v1/assist")
def assist(body: AssistPayload) -> AssistResponse:
    """One frame in, real predicted shapes out — the ``MEDIA_ASSIST_URL`` contract."""
    models: Models = app.state.models
    image = _fetch_frame(app.state.http, body.image_url)
    prompt = (body.prompt or "").strip()
    if prompt:
        shapes = models.detect(image, prompt, body.region)
        used = models.settings.detector_model
    elif body.region is not None:
        shapes = models.segment(image, body.region, label="object")
        used = models.settings.segmenter_model
    else:
        raise HTTPException(status_code=422, detail="need a prompt (detect) or a region (segment)")
    logger.info("assist %s → %d shape(s)", used, len(shapes))
    return AssistResponse(shapes=shapes, model=used)


if __name__ == "__main__":
    import uvicorn

    settings = Settings()
    uvicorn.run(app, host=settings.host, port=settings.port)
