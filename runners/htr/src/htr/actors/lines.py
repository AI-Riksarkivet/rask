"""LineActor — YOLO line detection within regions. Ray Data map_batches actor.

Reads `image_bytes` + `regions`, writes `lines` (flat list[Line] per page).
Polygons stored on Line.abs_polygon when YOLO segmentation produces masks; else 4-corner bbox.
"""

import io
import logging
import os

import numpy as np
from PIL import Image

from htr._columns import pack, unpack
from htr.preprocessing import crop_region
from htr.schemas import Line


logger = logging.getLogger(__name__)

#: The Hugging Face revision every weight load pins to. ``main`` is a MOVING POINTER: an upstream
#: push silently changes what this actor loads, so the identical pipeline over the identical pages
#: produces DIFFERENT transcriptions while lineage records the same model name. For an archive
#: publishing machine-generated readings of historical records, that is the provenance claim that
#: matters most. Override per-deployment with ``RASK_HTR_MODEL_REVISION``; pin it to a commit sha
#: before any run whose output will be published.
MODEL_REVISION = os.environ.get("RASK_HTR_MODEL_REVISION", "main")


class LineActor:
    def __init__(self, model: str = "Riksarkivet/yolov9-lines-within-regions-1", **_unused) -> None:
        self.model_name = model
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from huggingface_hub import hf_hub_download, list_repo_files
        from ultralytics import YOLO

        # Ultralytics' YOLO() doesn't resolve HF Hub repo IDs — fetch the .pt
        # file ourselves and pass the local path.
        pt_files = [f for f in list_repo_files(self.model_name, revision=MODEL_REVISION) if f.endswith(".pt")]
        if not pt_files:
            raise RuntimeError(f"No .pt file in {self.model_name}")
        model_file = hf_hub_download(repo_id=self.model_name, filename=pt_files[0], revision=MODEL_REVISION)
        self._model = YOLO(model_file)
        try:
            import torch

            if torch.cuda.is_available():
                self._model.to("cuda")
        except ImportError:
            pass
        logger.info("LineActor loaded: %s", self.model_name)

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        self._ensure_model()
        assert self._model is not None  # _ensure_model() guarantees this
        # Drop rows where line detection fails so a single bad page doesn't
        # kill the chunk. Keep all parallel arrays aligned.
        keys = batch.get("key")
        out_keys: list = []
        out_bytes: list = []
        out_regions: list = []
        out_lines: list = []
        for i, (img_bytes, regions_blob) in enumerate(zip(batch["image_bytes"], batch["regions"], strict=True)):
            try:
                image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                regions = unpack(regions_blob)
                page_lines: list[Line] = []
                for r in regions:
                    crop = crop_region(image, r.x, r.y, r.w, r.h)
                    results = self._model(crop, verbose=False)
                    for res in results:
                        if res.boxes is None:
                            continue
                        # YOLOv9-seg variants return mask polygons; bbox-only variants don't.
                        # When masks exist, store the absolute polygon on each Line so the ALTO
                        # serializer emits real Shape/Polygon points instead of bbox corners.
                        polys = res.masks.xy if res.masks is not None else None
                        for j, box in enumerate(res.boxes):
                            lx1, ly1, lx2, ly2 = box.xyxy[0].cpu().numpy().tolist()
                            abs_polygon = None
                            if polys is not None and j < len(polys):
                                poly = polys[j]
                                abs_polygon = [(float(p[0]) + r.x, float(p[1]) + r.y) for p in poly]
                            page_lines.append(
                                Line(
                                    x=float(lx1),
                                    y=float(ly1),
                                    w=float(lx2 - lx1),
                                    h=float(ly2 - ly1),
                                    confidence=float(box.conf[0]),
                                    abs_x=float(lx1 + r.x),
                                    abs_y=float(ly1 + r.y),
                                    abs_polygon=abs_polygon,
                                )
                            )
                page_lines.sort(key=lambda line: (line.abs_y, line.abs_x))
            except Exception as exc:
                k = keys[i] if keys is not None else f"<row {i}>"
                logger.warning("LineActor skipped %s: %s", k, exc)
                continue
            if keys is not None:
                out_keys.append(keys[i])
            out_bytes.append(img_bytes)
            out_regions.append(regions_blob)
            out_lines.append(page_lines)
        out: dict[str, np.ndarray] = {
            "image_bytes": np.array(out_bytes, dtype=object),
            "regions": np.array(out_regions, dtype=object),
            "lines": np.array([pack(ls) for ls in out_lines], dtype=object),
        }
        if keys is not None:
            out["key"] = np.array(out_keys, dtype=object)
        return out
