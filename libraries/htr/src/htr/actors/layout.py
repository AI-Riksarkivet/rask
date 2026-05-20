"""LayoutActor — YOLO region detection. Ray Data map_batches actor.

One row per page. Reads `image_bytes`, writes `regions` (list[Region])."""

import io
import logging

import numpy as np
from PIL import Image

from htr._columns import pack
from htr.schemas import Region


logger = logging.getLogger(__name__)


class LayoutActor:
    def __init__(self, model: str = "Riksarkivet/yolov9-regions-1", **_unused) -> None:
        self.model_name = model
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        from huggingface_hub import hf_hub_download, list_repo_files
        from ultralytics import YOLO

        # Ultralytics' YOLO() doesn't resolve HF Hub repo IDs — fetch the .pt
        # file ourselves and pass the local path.
        pt_files = [f for f in list_repo_files(self.model_name) if f.endswith(".pt")]
        if not pt_files:
            raise RuntimeError(f"No .pt file in {self.model_name}")
        model_file = hf_hub_download(repo_id=self.model_name, filename=pt_files[0])
        self._model = YOLO(model_file)
        try:
            import torch

            if torch.cuda.is_available():
                self._model.to("cuda")
        except ImportError:
            pass
        logger.info("LayoutActor loaded: %s", self.model_name)

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        self._ensure_model()
        # Drop rows where YOLO refuses (corrupt JPEG, OOM, etc.) so a single
        # bad page doesn't kill the chunk. Keep `key` and `image_bytes`
        # aligned with the new `regions` so downstream stages see consistent
        # parallel arrays.
        keys = batch.get("key")
        out_keys: list = []
        out_bytes: list = []
        out_regions: list = []
        for i, img_bytes in enumerate(batch["image_bytes"]):
            try:
                image = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                results = self._model(image, verbose=False)
            except Exception as exc:
                k = keys[i] if keys is not None else f"<row {i}>"
                logger.warning("LayoutActor skipped %s: %s", k, exc)
                continue
            page_regions: list[Region] = []
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().tolist()
                    page_regions.append(
                        Region(
                            x=float(x1),
                            y=float(y1),
                            w=float(x2 - x1),
                            h=float(y2 - y1),
                            confidence=float(box.conf[0]),
                            label=self._model.names[int(box.cls[0])] if box.cls is not None else None,
                        )
                    )
            if keys is not None:
                out_keys.append(keys[i])
            out_bytes.append(img_bytes)
            out_regions.append(page_regions)
        out: dict[str, np.ndarray] = {
            "image_bytes": np.array(out_bytes, dtype=object),
            "regions": np.array([pack(rs) for rs in out_regions], dtype=object),
        }
        if keys is not None:
            out["key"] = np.array(out_keys, dtype=object)
        return out
