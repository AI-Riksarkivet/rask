"""ALTO XML 4.4 serialization from PageWithText."""

from collections.abc import Sequence
from datetime import UTC, datetime
from importlib.resources import files
from io import BytesIO
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader
from PIL import Image

from htr.alto.word_segment import segment_words
from htr.models import COMMIT_SHA, PIPELINE_MODELS, ModelRef
from htr.schemas import PageWithText, TranscribedLine


_TEMPLATES_DIR = str(files("htr.alto").joinpath("templates"))

_METADATA = {
    "Author-email": "ai@riksarkivet.se",
    "Name": "htr",
    "Version": "0.1.0",
    "Summary": "HTR pipeline stages for Riksarkivet historical documents",
}


def _build_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701
    )


def _line_polygon_points(tl: TranscribedLine) -> str:
    """Return ALTO polygon POINTS string for a line.

    Uses the absolute polygon if available, else falls back to the line bbox corners.
    """
    line = tl.line
    if line.abs_polygon:
        return " ".join(f"{int(px)},{int(py)}" for px, py in line.abs_polygon)
    x1, y1 = int(line.abs_x), int(line.abs_y)
    x2, y2 = int(line.abs_x + line.w), int(line.abs_y + line.h)
    return f"{x1},{y1} {x2},{y1} {x2},{y2} {x1},{y2}"


class _Poly:
    """Polygon shim: stringifies to ALTO POINTS format and exposes `.bbox` for the template."""

    def __init__(self, points: str, bbox_ns: SimpleNamespace):
        self.bbox = bbox_ns
        self._points = points

    def __str__(self) -> str:
        return self._points


def _line_bbox(tl: TranscribedLine) -> SimpleNamespace:
    line = tl.line
    return SimpleNamespace(
        xmin=int(line.abs_x),
        ymin=int(line.abs_y),
        width=int(line.w),
        height=int(line.h),
    )


def _build_document_shim(page_with_text: PageWithText, *, emit_words: bool) -> SimpleNamespace:
    """Build a SimpleNamespace tree shaped like the legacy Document/Region/Text model.

    The template iterates `document.regions` as a flat list of "lines" (matching how
    the legacy `assemble_alto` flattened all lines into Document.regions). Each line
    needs `polygon` (str-able + .bbox), `transcription` (list with .text/.confidence),
    and optionally `words` (list with .x/.y/.w/.h/.text/.confidence) for per-word
    `<String>` emission.
    """
    image = Image.open(BytesIO(page_with_text.page.image_bytes))
    image_width, image_height = image.size
    image_name = page_with_text.page.key.rsplit("/", 1)[-1]

    line_nodes: list[SimpleNamespace] = []
    for _region, tlines in page_with_text.region_lines:
        for tl in tlines:
            polygon_str = _line_polygon_points(tl)
            bbox = _line_bbox(tl)
            # Jinja calls str() on the polygon when interpolated bare; SimpleNamespace
            # doesn't support __str__ override via attribute, so we use _Poly below.
            poly = _Poly(polygon_str, bbox)

            text_node = SimpleNamespace(text=tl.text, confidence=tl.confidence)

            words_list: list[SimpleNamespace] = []
            if emit_words and tl.text.strip():
                segmented = segment_words(tl.line, tl.text, tl.confidence)
                # Compute the inter-word space width once (uniform across words in a line).
                stripped = tl.text.strip().split()
                if len(stripped) > 1:
                    char_total = sum(len(w) for w in stripped) + len(stripped) - 1
                    space_w = tl.line.w / char_total if char_total else 0.0
                else:
                    space_w = 0.0
                words_list = [
                    SimpleNamespace(
                        x=w.x,
                        y=w.y,
                        w=w.w,
                        h=w.h,
                        text=w.text,
                        confidence=w.confidence,
                        space_w=space_w,
                    )
                    for w in segmented
                ]

            line_nodes.append(
                SimpleNamespace(
                    polygon=poly,
                    transcription=[text_node],
                    words=words_list,
                )
            )

    image_ns = SimpleNamespace(width=image_width, height=image_height)
    return SimpleNamespace(image_name=image_name, image=image_ns, regions=line_nodes)


def serialize_alto(page_with_text: PageWithText, *, emit_words: bool = True, models: Sequence[ModelRef] = PIPELINE_MODELS) -> str:
    """Render PageWithText as an ALTO XML 4.4 string.

    When emit_words=True, each line is broken into <String> elements via geometric
    word segmentation. When False, each line emits a single <String> covering the
    whole line bbox.

    ``models`` are recorded as one ``<Processing>`` block each, carrying the step and
    ``model=<repo>@<revision>`` as ``processingStepSettings``. Until this, a published ALTO named the
    SOFTWARE (`htr` 0.1.0) and no model at all — so a reader holding a machine-generated reading of a
    historical record could not tell which weights produced it, and two readings from different model
    revisions were indistinguishable. ALTO 4.4 has the slot; we simply were not filling it. Pass an
    empty sequence for a run whose models are genuinely unknown — an absent block reads as "not
    recorded", which is honest; a wrong one does not.
    """
    document = _build_document_shim(page_with_text, emit_words=emit_words)

    # Compute page confidence as mean of line confidences (matches legacy behavior).
    confidences: list[float] = [tl.confidence for _region, tlines in page_with_text.region_lines for tl in tlines]
    page_confidence = sum(confidences) / len(confidences) if confidences else None

    env = _build_env()
    template = env.get_template("alto-4-4")
    return template.render(
        document=document,
        metadata=_METADATA,
        timestamp=datetime.now(tz=UTC).isoformat(),
        page_confidence=page_confidence,
        models=models,
        commit_sha=COMMIT_SHA,
    )
