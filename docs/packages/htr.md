# htr (`runners/htr`)

!!! warning "This is not `packages/htr` — that path does not exist"

    The HTR library lives in the **sealed runner** `runners/htr`, alongside the Ray Data
    pipeline that drives it. `runners/*` is matched by **no workspace glob** and carries its
    **own `pyproject.toml` and `uv.lock`**, so torch/htrflow/ultralytics/transformers never
    enter the fleet's resolution. Run it with `uv run --project runners/htr runner`; in-cluster
    the Ray image ships the console script on `PATH`. See `.claude/skills/rask-architecture`.

The HTR library: Ray Data actors, the data schemas that flow between them, the
ALTO 4.4 serializer, and geometry helpers. It is **hardware-agnostic** — pool
sizes and GPU fractions are applied by the runner, not baked into the actors.

→ Auto-generated symbol docs: **[API reference](../reference/htr.md)**.

## Public API

`from htr import …` exposes **only the schemas** (frozen dataclasses):
`PageImage`, `Region`, `Line`, `Word`, `TranscribedLine`, `PageWithRegions`,
`PageWithLines`, `PageWithText`. Actors and helpers are imported from their
submodules (`htr.actors.layout`, `htr.alto.serialize`, `htr.geometry`).

## Pipeline stages (actors)

Each actor is a config-only class that lazily loads its model on first call and
maps `dict[str, np.ndarray]` batches (one row per page).

| Actor | Module | Role | Resource |
|---|---|---|---|
| `PageLoaderActor` | `actors/io.py` | Read bytes from a `storage.Source` (16-thread GET pool); validate with PIL, drop unreadable pages. | CPU / network |
| `PrefetchActor` | `actors/io.py` | Cache-warmer — calls `source.read` for the IIIF→S3 write-through side effect. | CPU / network |
| `LayoutActor` | `actors/layout.py` | YOLO region detection (`Riksarkivet/yolov9-regions-1`). | `num_gpus=0.001` |
| `LineActor` | `actors/lines.py` | YOLO line detection within each region crop (`yolov9-lines-within-regions-1`); stores absolute polygons when the model emits masks; sorts by `(y, x)`. | `num_gpus=0.001` |
| `TranscribeActor` | `actors/transcription.py` | In-process TrOCR (bypassed in production — see below). | GPU |
| `AltoExportActor` | `actors/alto_export.py` | Regroup transcribed lines under regions, build `PageWithText`, serialize ALTO. | CPU |
| `AltoWriterActor` | `actors/io.py` | Write each `(output_key, alto_xml)` to a `storage.Sink`. | CPU |
| `FakeAltoActor` | `actors/fake.py` | Emit stub ALTO for no-GPU smoke tests. | CPU |

!!! info "Transcription runs on Ray Serve, not in-actor"
    In production the runner replaces `TranscribeActor` with `TranscribeViaServe`
    (a CPU-only step that crops + length-buckets lines and calls a warm TrOCR
    **Serve** deployment over a handle). The in-package `TranscribeActor` and the
    Serve `TranscribeService` share model-setup code — including a
    transformers ≥5.6 meta-tensor workaround for the TrOCR positional embedding.

## Schemas

The logical flow is `PageImage → PageWithRegions → (lines) → PageWithText`. Note
that actors pass **parallel object-dtype columns** (`key`, `image_bytes`,
`regions`, `lines`, `transcribed`, `output_key`, `alto_xml`) where list-valued
columns are pickle blobs (see `_columns.py`); `PageWithText` is reconstructed
only inside `AltoExportActor`.

Key fields: `Region(x, y, w, h, confidence, label, polygon)`,
`Line(x, y, w, h, abs_x, abs_y, abs_polygon, confidence)`,
`TranscribedLine(line, text, confidence, words)`.

## ALTO serialization

`htr.alto.serialize.serialize_alto(page_with_text, emit_words=True)` renders
**ALTO 4.4** via a Jinja2 template (`alto/templates/alto-4-4`). Lines emit a real
`<Shape><Polygon>` when `abs_polygon` is present, else the four bbox corners.
Word boxes come from `alto/word_segment.py` — **geometric** segmentation
(splitting line width by character count), not a model. Line/page confidence is
`exp(mean per-token log-prob)`.

## Geometry & preprocessing

- `htr/geometry.py` — `Point`, `Bbox`, `Polygon`, plus mask↔polygon↔bbox
  converters (`mask2polygon` uses contour + Douglas-Peucker).
- `htr/preprocessing.py` — YOLO/RTMDet letterboxing, TrOCR normalization,
  `crop_region` / `crop_polygon`.
- `htr/reading_order.py` — printspace + two-page reading-order estimation (ported
  from htrflow; **not currently wired** into the actor path).

## Gotchas

- **Model weights are hardcoded HF repos** and the YOLO `.pt` is fetched via
  `hf_hub_download` (Ultralytics can't resolve HF repo IDs). The private TrOCR
  model needs `HF_TOKEN` on deployed clusters.
- **Resource hints live in the runner** (`pipeline.py`), not here — `num_gpus`,
  pool sizes, `batch_size`. The `0.001` GPU fraction is a placement token.
- **Line ordering happens twice** with different logic (`LineActor` sort vs.
  `AltoExportActor` containment regroup).
