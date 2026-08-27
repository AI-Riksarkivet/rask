"""INSID3 assist backend — the SECOND producer behind the assist contract (the pluggability proof).

Same wire contract as ``runners/assist``: one POST ``/v1/assist`` with ``{image_url, prompt,
region}`` returns ``{shapes: [...]}``. The mode is different — **in-context propagation**: the
drawn ``region`` becomes a REFERENCE mask on the unit's own frame, and INSID3 (training-free,
one frozen DINOv3 backbone — visinf/insid3, CVPR'26) segments everything similar on that frame.
"Label one, find the rest", which neither the text-prompt (GroundingDINO) nor the geometry-prompt
(SAM) producer covers.

Frames are fetched from ``ASSIST_FRAME_BASE`` + the payload's relative ``image_url`` — absolute
URLs are rejected, so this server can only ever read from the viewer it is deployed beside (the
same SSRF stance as ``runners/assist``).

The INSID3 code is consumed from a checkout named by ``INSID3_CODE`` (the upstream repo is a flat
research layout, not an installable package); DINOv3 weights are Meta-gated — fetch the HF
safetensors after license acceptance and run ``convert_weights.py`` to produce the original-format
checkpoint ``torch.hub`` strict-loads (the conversion is verified by that strict load).
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
from typing import Any

import cv2
import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI
from PIL import Image, ImageDraw
from pydantic import BaseModel


logger = logging.getLogger(__name__)

FRAME_BASE = os.environ.get("ASSIST_FRAME_BASE", "http://localhost:8101")
INSID3_CODE = os.environ.get("INSID3_CODE", "/opt/insid3")
MODEL_SIZE = os.environ.get("INSID3_MODEL_SIZE", "small")
IMAGE_SIZE = int(os.environ.get("INSID3_IMAGE_SIZE", "1024"))
DEVICE = os.environ.get("INSID3_DEVICE", "cuda")
#: A bare click (zero-size region) grows to this box — the reference needs area to carry a concept.
CLICK_PATCH = 120


class Region(BaseModel):
    x: float
    y: float
    width: float
    height: float


class AssistRequest(BaseModel):
    image_url: str
    prompt: str | None = None
    region: Region | None = None


_model_lock = threading.Lock()
_model: Any = None


def _get_model() -> Any:
    global _model
    if _model is None:
        sys.path.insert(0, INSID3_CODE)
        os.chdir(INSID3_CODE)  # torch.hub weights path in models/__init__ is checkout-relative
        from models import build_insid3  # noqa: PLC0415 - import lives in the checkout

        _model = build_insid3(model_size=MODEL_SIZE, image_size=IMAGE_SIZE, device=DEVICE)
        logger.info("insid3 ready (%s @%d on %s)", MODEL_SIZE, IMAGE_SIZE, DEVICE)
    return _model


def _fetch_frame(image_url: str) -> Image.Image:
    if image_url.startswith(("http://", "https://")):
        raise ValueError("absolute image URLs are rejected — frames come from the co-deployed viewer only")
    resp = httpx.get(f"{FRAME_BASE.rstrip('/')}{image_url}", timeout=15.0)
    resp.raise_for_status()
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def _mask_to_shapes(mask: np.ndarray, label: str) -> list[dict[str, Any]]:
    """Predicted mask → polygon shapes, one per connected component big enough to matter."""
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    shapes: list[dict[str, Any]] = []
    for contour in contours:
        if cv2.contourArea(contour) < 64:
            continue
        eps = 0.01 * cv2.arcLength(contour, True)
        poly = cv2.approxPolyDP(contour, eps, True).reshape(-1, 2)
        x, y, w, h = cv2.boundingRect(contour)
        shapes.append(
            {
                "shape_type": "polygon",
                "x": float(x),
                "y": float(y),
                "width": float(w),
                "height": float(h),
                "polygon": [float(v) for v in poly.reshape(-1)],
                "label": label,
                "confidence": 0.5,  # INSID3 emits no per-instance score; a fixed honest mid value
            }
        )
    return shapes


app = FastAPI(title="insid3-assist")


@app.get("/livez")
async def livez() -> dict[str, bool]:
    """`async def` so liveness answers on the event loop — the threadpool on this pod is where
    inference runs, and a sync handler would queue liveness behind the workload."""
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict[str, bool]:
    """Plain `def` DELIBERATELY, and it must stay one: `_get_model()` loads weights on first call.

    That blocking load is what makes this a real readiness check rather than a constant — but it
    belongs on the threadpool. Made `async def` to match its sibling above, the first probe after a
    cold start would hold the event loop for the whole load and stall every other request on the pod.
    """
    with _model_lock:
        _get_model()
    return {"ok": True}


@app.post("/v1/assist")
def assist(body: AssistRequest) -> dict[str, Any]:
    frame = _fetch_frame(body.image_url)
    r = body.region
    if r is None or r.width <= 0 or r.height <= 0:
        cx = r.x if r else frame.width / 2
        cy = r.y if r else frame.height / 2
        box = (cx - CLICK_PATCH / 2, cy - CLICK_PATCH / 2, cx + CLICK_PATCH / 2, cy + CLICK_PATCH / 2)
    else:
        box = (r.x, r.y, r.x + r.width, r.y + r.height)

    ref_mask = Image.new("L", frame.size, 0)
    ImageDraw.Draw(ref_mask).rectangle([int(v) for v in box], fill=255)

    with _model_lock:  # one warm model; interactive bursts serialize, same as runners/assist
        model = _get_model()
        model.set_reference(frame, ref_mask)
        model.set_target(frame)
        pred = model.segment()

    pred = pred.detach().cpu().numpy() if hasattr(pred, "detach") else np.asarray(pred)
    label = (body.prompt or "similar").strip()
    return {"shapes": _mask_to_shapes(pred > 0, label)}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8111")))
