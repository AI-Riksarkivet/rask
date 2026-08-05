"""Parse an HTR ALTO document into the gold contract's payload columns (#88 step 2).

The transcribe compute stays in Ray Serve; what enters the medallion is the ALTO it returns. This
module turns that document into exactly the payload half of ``GOLD_CONTRACT_COLUMNS`` — and it
imports the contract at module scope with a load-bearing assertion, which is the moment that
contract stops being "imported by nothing in production, asserted equal to itself by two tests".

PROVENANCE COMES FROM THE DOCUMENT, NEVER FROM CONFIG. The ALTO carries which models read the page
(``model=<repo>@<revision>`` per #89, or htrflow's ``model_settings`` step descriptions) and which
build ran them (``commit=<sha>``, #88 step 1). A mover-side declaration would be a REQUEST — if the
runner image is stale, the record lies. Parsing the run's own artefact cannot.

Both lane shapes are handled, because both are OUR documents: the actor lane's flat
``<Processing ID="model_*">`` blocks, and the /htrflow lane's ``block<i>_line<j>`` layout with
word- or line-level ``<String>`` elements. Absent provenance parses as absent (empty models, None
sha) — the serializers' silence-is-honest rule holds on the way back in.
"""

from __future__ import annotations

import re

# The input is our own Serve deployment's output. stdlib ElementTree resolves no external entities
# since 3.8, so the classic XXE vector does not exist here; the bandit rule predates that.
from xml.etree import ElementTree as ET  # noqa: S405

from pydantic import BaseModel, Field

from medallion.schemas.htr import GOLD_CONTRACT_COLUMNS


_NS = {"alto": "http://www.loc.gov/standards/alto/ns-v4#"}

#: What THIS module produces, per page. The caller supplies the rest: ``volume_id`` and
#: ``source_rowid`` come from the bronze row being transcribed, ``lineage`` is stamped by the
#: writer in the same commit as the data (R26).
PARSED_COLUMNS: tuple[str, ...] = (
    "page_key",
    "page_width",
    "page_height",
    "region_polygons",
    "line_polygons",
    "reading_order",
    "text",
    "confidences",
)
_CALLER_COLUMNS: tuple[str, ...] = ("volume_id", "source_rowid", "lineage")

# The contract becomes LOAD-BEARING here: if a column is added to or dropped from
# GOLD_CONTRACT_COLUMNS, this module fails at IMPORT — in the mover's pod, not in a test that
# asserts the tuple equals itself. A field dropped from gold is unrecoverable downstream (the ALTO
# serializer cannot reconstruct it), so the failure must be loud and early.
_covered = set(PARSED_COLUMNS) | set(_CALLER_COLUMNS)
if _covered != set(GOLD_CONTRACT_COLUMNS):
    raise ImportError(
        f"htr_parse no longer covers the gold contract: missing={set(GOLD_CONTRACT_COLUMNS) - _covered}, extra={_covered - set(GOLD_CONTRACT_COLUMNS)}"
    )


class ParsedPage(BaseModel):
    """One page's gold payload, plus the provenance parsed out of the document itself."""

    model_config = {"frozen": True}

    page_key: str
    page_width: int
    page_height: int
    #: One POINTS string per TextBlock — the polygon when the document carries a Shape, else the
    #: bbox rectangle. Same convention the actor lane's serializer writes.
    region_polygons: list[str] = Field(default_factory=list)
    line_polygons: list[str] = Field(default_factory=list)
    #: Line indices in READING order (positions into ``text``/``confidences``/``line_polygons``).
    reading_order: list[int] = Field(default_factory=list)
    #: One transcription per line, words joined with single spaces where the line is word-split.
    text: list[str] = Field(default_factory=list)
    #: Per-line confidence: the line String's WC, or the mean of its word WCs.
    confidences: list[float] = Field(default_factory=list)
    #: ``repo@revision`` per model block, AS THE DOCUMENT RECORDS THEM. Empty when unrecorded.
    models: list[str] = Field(default_factory=list)
    #: The runner build's commit, or None when the document carries no build block.
    commit_sha: str | None = None


def _bbox_points(el: ET.Element) -> str:
    x = int(float(el.get("HPOS", "0")))
    y = int(float(el.get("VPOS", "0")))
    w = int(float(el.get("WIDTH", "0")))
    h = int(float(el.get("HEIGHT", "0")))
    return f"{x},{y} {x + w},{y} {x + w},{y + h} {x},{y + h}"


def _polygon_points(el: ET.Element) -> str:
    """The element's Shape/Polygon POINTS, else its bbox corners — one rule for blocks and lines."""
    poly = el.find("alto:Shape/alto:Polygon", _NS)
    points = poly.get("POINTS", "") if poly is not None else ""
    return points or _bbox_points(el)


def _line_text_and_confidence(line: ET.Element) -> tuple[str, float]:
    """A line's transcription and confidence from its ``<String>`` children.

    The /htrflow template emits EITHER one line-level String (CONTENT + WC) or one String per word
    — never both. Word WCs are averaged; a line with no Strings (or empty CONTENT) is an empty
    transcription with confidence 0.0, kept rather than dropped: cardinality against
    ``line_polygons`` is the invariant every consumer of this data relies on (the blob-v2 lesson).
    """
    strings = line.findall("alto:String", _NS)
    words = [s.get("CONTENT", "") for s in strings]
    confidences = [float(s.get("WC", "0") or "0") for s in strings if s.get("WC")]
    text = " ".join(w for w in words if w) if len(words) > 1 else (words[0] if words else "")
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return text, confidence


#: The provenance shapes our serializers write (#89/#88). THREE, and the third was live-caught:
#: the fixture GUESSED htrflow rendered a repr-style ``{'model': ..., 'revision': ...}`` — the real
#: deployed document (2026-08-05, R0002231_00001) renders FLAT ``model=<repo>,
#: model_version=<resolved-sha>`` — and the resolved sha is strictly better provenance than the
#: requested revision, because it is what the load actually pinned to. Against that real shape the
#: first parser returned models=[] — silently, which is exactly the failure class #89 exists for.
_MODEL_SETTING_RE = re.compile(r"\bmodel=(\S+@\S+)")
_COMMIT_SETTING_RE = re.compile(r"\bcommit=([0-9a-fA-F]+)")
_HTRFLOW_LIVE_RE = re.compile(r"\bmodel=([^,\s)@]+), model_version=([0-9a-fA-F]+)")
_HTRFLOW_REPR_MODEL_RE = re.compile(r"'model':\s*'([^']+)'")
_HTRFLOW_REPR_REVISION_RE = re.compile(r"'revision':\s*'([^']+)'")


def _provenance(root: ET.Element) -> tuple[list[str], str | None]:
    models: list[str] = []
    commit: str | None = None
    for processing in root.findall(".//alto:Description/alto:Processing", _NS):
        for tag in ("processingStepSettings", "processingStepDescription"):
            for el in processing.findall(f"alto:{tag}", _NS):
                content = el.text or ""
                models.extend(_MODEL_SETTING_RE.findall(content))
                if (m := _COMMIT_SETTING_RE.search(content)) is not None:
                    commit = m.group(1)
                # The LIVE htrflow shape: flat `model=<repo>, model_version=<resolved-sha>` — the
                # adjacency pairs them, so two steps cannot cross-contaminate.
                models.extend(f"{repo}@{sha}" for repo, sha in _HTRFLOW_LIVE_RE.findall(content))
                # The repr-style shape (a config echoed verbatim renders this way) — kept, paired
                # WITHIN one description for the same no-cross-contamination reason.
                repo = _HTRFLOW_REPR_MODEL_RE.search(content)
                if repo is not None and "@" not in repo.group(1):
                    rev = _HTRFLOW_REPR_REVISION_RE.search(content)
                    models.append(f"{repo.group(1)}@{rev.group(1) if rev else 'main'}")
    # De-duplicated preserving order: htrflow renders one step description per PIPELINE step, and
    # the two segmentation steps share nothing, but a re-serialized document may repeat blocks.
    return list(dict.fromkeys(models)), commit


def parse_alto(xml: str) -> ParsedPage:
    """One ALTO document → one gold payload row. Raises ``ValueError`` on a document that is not
    ALTO-shaped — a mover must fail the page loudly, not write a half-parsed row."""
    try:
        root = ET.fromstring(xml)  # noqa: S314 — our own Serve's output; stdlib ET resolves no entities
    except ET.ParseError as exc:
        raise ValueError(f"not parseable as ALTO XML: {exc}") from exc

    page = root.find(".//alto:Layout/alto:Page", _NS)
    if page is None:
        raise ValueError("no <Page> element — not an ALTO layout document")

    file_name = root.find(".//alto:sourceImageInformation/alto:fileName", _NS)
    page_key = (file_name.text or "").strip() if file_name is not None else ""

    region_polygons: list[str] = []
    line_polygons: list[str] = []
    text: list[str] = []
    confidences: list[float] = []
    line_index_by_id: dict[str, int] = {}

    for block in page.findall(".//alto:TextBlock", _NS):
        region_polygons.append(_polygon_points(block))
        for line in block.findall("alto:TextLine", _NS):
            line_id = line.get("ID", "")
            if line_id:
                line_index_by_id[line_id] = len(text)
            line_polygons.append(_polygon_points(line))
            line_text, line_conf = _line_text_and_confidence(line)
            text.append(line_text)
            confidences.append(line_conf)

    # Reading order from the document's own OrderedGroup refs; identity order when it has none
    # (the actor lane's flat template) or when a ref names a line we did not see.
    refs = [el.get("REF", "") for el in root.findall(".//alto:ReadingOrder//alto:ElementRef", _NS)]
    ordered = [line_index_by_id[r] for r in refs if r in line_index_by_id]
    reading_order = ordered if len(ordered) == len(text) else list(range(len(text)))

    models, commit_sha = _provenance(root)
    return ParsedPage(
        page_key=page_key,
        page_width=int(float(page.get("WIDTH", "0"))),
        page_height=int(float(page.get("HEIGHT", "0"))),
        region_polygons=region_polygons,
        line_polygons=line_polygons,
        reading_order=reading_order,
        text=text,
        confidences=confidences,
        models=models,
        commit_sha=commit_sha,
    )
