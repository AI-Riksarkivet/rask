"""AltoExportActor — render PageWithText into ALTO XML bytes. Ray Data map_batches actor.

Reads `key` + `image_bytes` + `regions` + `transcribed`, writes `output_key` + `alto_xml`.

Region grouping: legacy `LineSegmentationStage` produced region-grouped lines
(`PageWithLines.region_lines`); the new `LineActor` flattens. We re-group here
by bbox containment (line center inside region rect)."""

from collections.abc import Sequence
from typing import Any

import numpy as np

from htr._columns import unpack
from htr.alto.serialize import serialize_alto
from htr.models import PIPELINE_MODELS, ModelRef
from htr.schemas import PageImage, PageWithText, Region, TranscribedLine


def _group_by_region(regions: list[Region], transcribed: list[TranscribedLine]) -> list[tuple[Region, list[TranscribedLine]]]:
    """Group transcribed lines under their parent region by bbox containment.
    For pages with no regions, return an empty list."""
    if not regions:
        return []
    out: list[tuple[Region, list[TranscribedLine]]] = [(r, []) for r in regions]
    for t in transcribed:
        line = t.line
        cx, cy = line.abs_x + line.w / 2, line.abs_y + line.h / 2
        for region, bucket in out:
            if region.x <= cx <= region.x + region.w and region.y <= cy <= region.y + region.h:
                bucket.append(t)
                break
    return out


class AltoExportActor:
    def __init__(self, emit_words: bool = True, models: Sequence[ModelRef] = PIPELINE_MODELS, **_unused: Any) -> None:  # noqa: ANN401
        self.emit_words = emit_words
        # Which models the run loaded, for the ALTO's provenance blocks. Defaulted rather than
        # threaded from `runner.pipeline` because the pipeline constructs its actors from these same
        # `htr.models` constants — one declaration, so the record cannot drift from the load.
        self.models = models

    def __call__(self, batch: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        out_keys: list[str] = []
        out_xml: list[bytes] = []
        for key, img_bytes, regions_blob, transcribed_blob in zip(batch["key"], batch["image_bytes"], batch["regions"], batch["transcribed"], strict=True):
            page = PageImage(key=key, image_bytes=img_bytes)
            regions = unpack(regions_blob)
            transcribed = unpack(transcribed_blob)
            region_lines = _group_by_region(list(regions), list(transcribed))
            page_with_text = PageWithText(page=page, region_lines=region_lines)
            xml_str = serialize_alto(page_with_text, emit_words=self.emit_words, models=self.models)
            out_keys.append(key.rsplit(".", 1)[0] + ".xml")
            out_xml.append(xml_str.encode("utf-8"))
        batch["output_key"] = np.array(out_keys, dtype=object)
        batch["alto_xml"] = np.array(out_xml, dtype=object)
        return batch
