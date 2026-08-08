# open-anno-active — annotation, AI-assist and active-learning gaps against X-AnyLabeling (+Server) and ActiveLabelingSystem

Working findings, **2026-08-08**, against `HEAD 53440d3`. Comparison heads, all cloned and read at
source level this pass: **X-AnyLabeling 4.0.1** (`af2a0b7`, PyQt6 desktop, GPL-3.0),
**X-AnyLabeling-Server 0.0.12** (`8efd68c`, FastAPI inference sidecar, AGPL-3.0),
**ActiveLabelingSystem 0.2.1** (`f502c9d`, PyQt6 desktop). Unsettled work; this file is deleted
when the improvements land or are rejected. `docs/` is for settled architecture only.

**Evidence convention** (same as `open_ingest_design.md`): every claim carries one of:

- `path:line` — **read from source** this pass. Rask paths are repo-relative; comparison-repo
  paths carry a repo prefix (`XAL/`, `XAL-S/`, `ALS/`) and are pinned to the heads above.
- `UNVERIFIED` — an inference or an estimate. Named inline, never buried.

**Scope.** Improvements to OUR annotator plane only — the canvas (`@rask/engine`), the label model
(`@rask/labeling`), the zone (`microfrontends/annotator`), and the assist/train loop
(`services/annotator`, `services/medallion` train head). Deliberately **out of scope**: the
workflow/governance plane (task FSM, consensus, kappa, FGA, publish lineage) — none of the three
comparison systems has an equivalent, there is nothing to import, and this document is a gap list,
not a scoreboard.

**STATUS 2026-08-08 (evening) — landed today, in-session** (each pushed to `main` with unit + e2e
coverage; line references in §1–§3 predate these and are stale where they overlap):

- Mock producers emit `confidence`/`uncertainty`; the client stops dropping them; the review queue
  ranks for real (finding 0, the wire half; the RUNNER half remains open).
- Brush radius/output controls and the image-adjustments popover (finding 3, the "built-but-unwired
  engine surface" half; OBB tool + `unionMasks`/semantic mode remain open).
- Video object tracks consumed: interpolated boxes at the playhead, keyframe promotion,
  `+ keyframe` on the transport (finding 6's step 1; timeline rail + propagation remain open).
- The doccano interaction, three stages: spans as in-text highlights with one-click/digit-hotkey
  labeling (`TextSpanSurface`), the transcription as a lane of the image/video workspace
  (`DocumentTextLane`), and `kind=text` rendering the DOCUMENT as the canvas (`TextViewer` — no
  pixel medium, class legend as keymap).
- The video workspace carries the soundtrack as a labelable waveform lane (flat "timeline only"
  fallback for undecodable audio), bound to the same `<video>` clock.
- Item-level classification (§8e.1): ontology `tag` classes render a choice-chip bar over ANY
  modality's workspace (`ClassificationBar`); an active chip IS a `tag` row on the ordinary
  insert/undo/Save machinery. En route, a ghost-row fix: undone inserts now leave the sidebar
  projection too (`_suppressed`), not only the canvas.
- The compare view (§8e.4–5): `?view=compare` lays a multi-key item out as side-by-side CANDIDATES
  with the ontology's choices under each; a pick saves a version-guarded `tag` row onto the winning
  unit through the plain transport (no canvas controller, so no autosave race). Text panes render
  the document lines, image/AV panes the frame. Entry/exit are plain links from the page bar.
- SPAN OFFSETS FOLLOW THE TEXT (§8d.1 — the sharpest textual defect): correcting a transcription
  re-anchors every child span (prefix/suffix diff → shift, stretch, or DROP when the anchored text
  was edited away — never a clamped guess). A new `SpanEdit` save channel patches offsets by id
  (they are deliberately not editable fields), mirroring the temporal channel.
- FEW-SHOT PROPAGATION HAS A BUTTON: `PropagatePanel` in the sidebar — the picked annotations
  are the exemplars, a scope select (this item / entire dataset), one `insid3` job on the jobs
  seam with exemplar IDS, and an outcome line that reports the WIRE truth (job id + the honest
  mock warning), not the synchronous guess. The seam existed end to end and nothing called it.
- Fixed at the source along the way: floating pills overlapping the temporal strips; unseekable
  route-served media in e2e (206 ranges); the e2e mock treating any seeded body carrying a string
  `status` field as its {status, body} response shape (every seeded jobs route answered 500).

**The findings in one table**, ranked by leverage:

| # | Finding | Action shape |
| --- | --- | --- |
| **0** | Every assist path answers from an honest mock; ~~no producer emits confidence/uncertainty~~ (mock emits, wire keeps — LANDED); the train job trains column means. | Deploy a runner; drop a real trainer into the seam. |
| **1** | Interactive prompting is one-shot. XAL's session loop (±point, ±rect, finish-object) is the usability floor for SAM-style assist. | Wire interactive ops through `apply()`; add refinement UX + conf/IoU/class controls. |
| **2** | HTR — the estate's flagship workload — never reaches the annotator. The `htr` producer is batch-only and routes to nothing. | HTR pre-annotations as `status='prediction'` rows. |
| **3** | Rotated box is schema-complete and tool-absent; ~~brush controls, image adjustments~~ (LANDED); `unionMasks`, semantic mask mode still unwired. | OBB tool; finish mask ops. |
| **4** | Five annotations-table columns are written by nothing (`group_id`, `reading_order`, `difficult`, `links`, `metadata`); per-shape `attributes` never reach the canvas wire. | Extend `InsertRow`/save path; add inspector editing. |
| **5** | Auto-accept-with-QA-sampling and a multi-signal retrain policy are the two ideas worth lifting from ALS — with the caveat that ALS's own loop is largely simulated (§7). | Policy work on our accept path + train trigger. |
| **6** | ~~tracks.ts consumed by nothing~~ (interpolation + keyframes LANDED); timeline rail, tracker-assist and mask propagation remain. | Timeline UI; tracking/propagation runners. |
| **7** | The `scripts/` converters our import design defers to (COCO/YOLO ⇄ Arrow) do not exist. | Write them when the fine-tune loop starts. |
| **8** | **Task templates** (the Label-Studio labeling-config concept): `ontology.kind` exists and drives tools/classes, but does not select LAYOUT, and project creation offers no template gallery. The annotator is a general multimodal tool — templates are its missing organizing mechanism. | kind → workspace template; gallery pre-filling ontologies at project creation. |

---

## 0. The load-bearing finding: consumers without producers

**The active-learning UI is finished and renders dashes.** This is the single fact that reorders
everything else: most of what looks like "missing AL features" is actually one missing input.

The consumer side, all read this pass:

- The review queue orders **predictions first, highest uncertainty first** — the comment calls it
  "the active-learning order" (`frontend/microfrontends/annotator/src/lib/viewer/annotator.svelte.ts:262-270`).
- The sidebar table defaults its sort to `uncertainty` and treats `confidence`/`uncertainty` as
  numeric sort keys (`frontend/microfrontends/annotator/src/lib/viewer/layout/AnnotationTable.svelte:15-17,47`).
- The detail panel renders per-row uncertainty (`AnnotationDetail.svelte:152-153`); the list shows
  an uncertainty badge (`AnnotationList.svelte:64-69`).
- Accept / reject / reset-to-prediction and bulk accept/reject exist
  (`AnnotationDetail.svelte`, `BulkActions` — read this pass via the zone inventory).

The producer side:

- The assist endpoint routes to a model server **only when `MEDIA_ASSIST_URL` is set**; otherwise
  "a deterministic MOCK" (`services/annotator/src/annotator/api/v1/endpoints/assist.py:9`, `:307`
  `def _mock`). `default_configured` is exactly `bool(MEDIA_ASSIST_URL)` (`assist.py:114-115,204`).
  Nothing in the chart or env defaults sets it (UNVERIFIED for every deploy overlay; verified for
  the repo defaults).
- The batch-jobs plane has the same shape: real endpoint, idempotent job ids, corpus/scope
  selection (`services/annotator/src/annotator/api/v1/endpoints/jobs.py:81,99`) — mock-backed.
- The producer registry (`frontend/packages/labeling/src/producers.ts:48-84`) declares seven
  producers (`human`, `sam-click`, `insid3`, `grounding-dino`, `htr`, `vlm-judge`,
  `embed-propagate`); **zero are live by default**, and the UI says so honestly (the
  "mocked — needs runner" chip in `AiAssistBar.svelte`).
- The train head is real and version-pinning (`services/medallion/src/medallion/api/train.py:66-93`,
  gateway row `services/gateway/src/gateway/__init__.py:153`), the Ray job's registry commit is
  crash-safe (model version N == Lance version N, `scripts/ray_train_job.py:1-16`) — and the model
  it trains is per-column means: `train_demo_model`, self-described as "the seam a TorchTrainer
  drops into (D6)" (`scripts/ray_train_job.py:332-345`).

**Consequence.** The comparison with X-AnyLabeling is not "they have 200 models and we have 7" —
it is that our seven are registry rows and theirs run. One deployed runner serving SAM(2) + a
grounding detector, emitting `confidence`/`uncertainty` on every shape, converts the queue, the
sort, the badges, the accept flow and the batch plane from mocked to real **with no frontend work**.
Ray Serve beside `/transcribe` is the natural host (UNVERIFIED as a sizing claim; the pattern is
`runners/htr`'s). XAL-Server is the design reference for the serving half — a decorator+YAML model
registry whose `/v1/models` response drives the client's widgets
(`XAL-S/app/core/registry.py:14-34,69-83`, `XAL-S/app/api/models.py:8`) — and explicitly **not** a
dependency: it has no persistence, no projects, one static `==`-compared API key
(`XAL-S/app/core/middleware.py:11-55`), and holds every configured model resident from startup.
Our assist contract (ontology gating, `dropped[]`, redacted registry) is already the better half.

---

## 1. Canvas and annotation

### 1.1 Rotated box: schema-complete, tool-absent

`CommitShape.type` includes `'rotation'` with a documented radians field
(`frontend/packages/engine/src/interaction/types.ts:78,84-85`); the annotations table carries a
`rotation` column (`services/annotator/src/annotator/annotations/schema.py:143`); the wire
`InsertRow` carries `rotation` (`frontend/packages/labeling/src/annotations-client.ts`). **No tool
emits it** — the `Tool` union (`frontend/packages/engine/src/pixi/types.ts:22-32`) has ten modes
and none is a rotated box; `InteractionManager.select()` even routes oriented boxes to the rect
editor already. The whole feature is one tool class + one editor handle away.

XAL's treatment is the reference UX: draw axis-aligned then rotate, a drag rotation handle
(added 4.0.0-beta.12), `Z/X` coarse 1.0° / `C/V` fine 0.1° steps
(`XAL/anylabeling/views/labeling/widgets/canvas.py`, config
`XAL/anylabeling/configs/xanylabeling_config.yaml` `canvas.rotation.*`). Relevant to us for seals,
marginalia and skewed text blocks on scanned pages.

### 1.2 Built-but-unwired engine surface — finish before adding

Four things exist in the engine with **zero callers or zero UI**, verified this pass:

| What | Where it exists | What's missing |
| --- | --- | --- |
| Mask union | `unionMasks` (`frontend/packages/engine/src/maskOps.ts:32`) | Zero callers repo-wide. The file's own docstring calls cross-label pixel-exclusivity "a planned follow-up". |
| Semantic mask mode | `BrushTool` accepts `maskMode: 'instance'\|'semantic'` (`frontend/packages/engine/src/tools/BrushTool.ts`) | No store ever reads the mode. |
| Brush controls | `radius`, `output: 'mask'\|'polygon'` settable on the tool | Toolbar exposes only the eraser toggle (`AnnotatorToolbar.svelte`). No radius slider, no output select. |
| Image adjustments | `setImageAdjustments(brightness, contrast, saturation)` (`frontend/packages/engine/src/pixi/ImagePlugin.ts:305`) | No caller anywhere in `frontend/`. XAL ships a brightness/contrast dialog plus a persistent display panel and 16-bit grayscale support — table stakes for faded manuscript scans. |

These are cheaper than any new tool and each is already the hard half done.

### 1.3 Dead columns and the attributes gap

The annotations schema declares `group_id`, `reading_order`, `difficult`, `links`, `metadata`
(`services/annotator/src/annotator/annotations/schema.py:143`) — **no endpoint writes any of them**:
`NewAnnotation` doesn't carry them and `EDITABLE_FIELDS = (label, status, text, group, reviewer)`
excludes them. Two follow-ons:

- Canvas **relations persist only into the task draft**, never the annotations table — the `links`
  column stays empty while the canvas draws typed arrows.
- **`reading_order` is a first-class HTR need** (ALTO block/line order) and is currently
  unreachable from any UI or wire.

Separately, per-class typed **attributes** exist in the ontology
(`services/annotator/src/annotator/projects/ontology.py` — `OutputAttr {name, type, choices,
required}`), in the draft `Shape`, and in the published schema (`attributes` as `pa.json_()`,
`projects/publish.py`) — but the canvas wire `InsertRow` has no attributes field and the inspector
edits only label/status/text/group. XAL's config-driven per-shape attribute forms
(`XAL/anylabeling/views/labeling/label_widget.py`, uploadable validated schema) are the reference.
The fix is a wire field + an inspector form generated from the ontology we already validate against.

### 1.4 Tools worth adding, in order

1. **Magic wand / flood fill** — XAL 4.0.1's newest tool: click seeds a region, drag adjusts
   tolerance, right-click commits a polygon (`XAL/.../canvas.py`, config
   `canvas.magic_wand.{default_threshold, drag_sensitivity, luminance_weight, simplify_epsilon}`).
   High value on clean scan backgrounds; we removed live-wire deliberately
   (`frontend/packages/engine/src/interaction/InteractionManager.ts:80-85` documents why) and a
   flood fill is the cheap non-ML replacement that doesn't need OpenCV back.
2. **Quadrilateral** — 4 explicit corners; the natural shape for warped text lines and seals, and
   the shape PPOCR-style spotting emits (`XAL-S/app/schemas/shape.py` includes `quadrilateral`).
   We'd canonicalize it onto `polygon` (4 points) in `shape-types.ts` — no schema change.
3. **Copy / paste / duplicate** — we have none; XAL has Ctrl+C/V/D plus cross-image paste and
   optional system-clipboard interop. Composes with our existing insert overlay + undo stack
   (`annotator.svelte.ts` `UndoOp` already covers `insert`).
4. **Vertex eraser** (Alt-drag across vertices) and **wheel rect editing** (scroll inside = scale,
   outside = nudge nearest edge) — small canvas ergonomics with outsized labeling-speed effect.
5. **Per-shape lock / hide, and richer conversion** — we convert rect→polygon only (hotkey `P`,
   `InteractionManager.ts:295`); XAL's converter dialog covers 15 type-pairs batch-wide.
6. Circle/cuboid/keypoint-skeletons — real gaps, no archival use case pushing them. Defer.

### 1.5 Navigation and review ergonomics

Missing entirely, all cheap, all directly useful in review sessions: a **navigator/minimap**
(XAL's F9 panel renders shapes live with a viewport frame), **loop-through-objects**
(Ctrl+Shift+N — zoom to each shape in turn; pairs perfectly with our uncertainty-ordered queue),
**crosshair + cursor coordinate readout**, **digit shortcuts** binding 0-9 to (tool, label) pairs,
and **attribute-query search** over annotations (XAL: `label::`, `type::`, `score::[a,b]`,
`difficult::` — `XAL/anylabeling/views/labeling/utils/file_search.py`). Rebindable hotkeys are
already named as a gap by our own `/settings` stub (`routes/settings/+page.svelte`, `PlannedArea`).

---

## 2. AI-assisted labeling

### 2.1 Interactive dispatch: route it through `apply()`

`apply(op)` is the designed single seam (`annotator.svelte.ts` — human+interactive → local edits,
batch → jobs), but interactive **model** ops return
`{status:'unsupported', reason:'interactive … not wired yet'}` (`annotator.svelte.ts:1027`), and
SAM bypasses the seam entirely via the separate `assist()` path that arms `assistProducer` and
re-uses `RectTool` as a prompt gesture. INSID3 interactive (exemplar-conditioned segmentation) is
declared in the registry (`producers.ts:59-68`) and unreachable. Unifying on `apply()` is a
precondition for everything in §2.2 — refinement loops need op-level state, not a one-shot side
path.

### 2.2 Prompt refinement: the session loop is the floor

Ours is one-shot: one box (or a click grown server-side to a 120 px patch, `assist.py`) → one
result → rows land in the queue. XAL's loop is a session
(`XAL/anylabeling/services/auto_labeling/types.py` — `AUTOLABEL_ADD`/`AUTOLABEL_REMOVE` ×
point/rect; panel `XAL/.../auto_labeling.py`): **+point `Q` / −point `E`, +rect / −rect,
run, clear `B`, finish-object `F`** — the negative point is what makes SAM usable on touching
regions, which is the common case on manuscript pages. XAL-S carries the same marks contract over
the wire (`marks: {type: point|rectangle, label: 0|1, data}`) — our `AssistShape`/region contract
extends to it without a redesign (UNVERIFIED: exact wire-shape delta unchecked).

Also missing at this layer, all standard in XAL's panel: **confidence and IoU sliders, a class
filter, an output-mode select** (polygon/rect/contour — XAL's SAM2 wrapper,
`XAL/anylabeling/services/auto_labeling/segment_anything_2.py:55-61`), and a **replace-vs-append
toggle**. Ours has a prompt box and per-producer buttons.

### 2.3 Segment everything

Prompt-free full-page proposal generation: XAL runs SAM2 AMG chunked and cancellable with the full
knob set (`points_per_side`, `pred_iou_thresh`, `stability_score_thresh`, `box_nms_thresh`,
`min_mask_region_area` — `XAL/.../segment_anything_2.py:304+`). Notably **XAL-S does not expose
AMG** (`XAL-S/app/models/sam2/automatic_mask_generator.py` is vendored dead code) — if we build the
runner (§0) we should expose it from day one; it is the bulk-region bootstrap for layout labeling,
feeding straight into our existing bulk accept/reject.

### 2.4 HTR pre-annotations — the flagship connection

The estate exists to do HTR, and HTR output never reaches the annotator: the `htr` producer is
`executions: ['batch']` and routes to nothing (`producers.ts:84`); the pipeline writes ALTO which
`services/medallion/src/medallion/services/htr_parse.py` parses into the gold contract; the
annotations table and the gold tier never exchange a row. The improvement: an assist backend (or a
bronze-arrival hook — UNVERIFIED which is cheaper) that surfaces text-line polygons + transcriptions
as `status='prediction'`, `source='model:htr'` rows with real per-line confidence (TrOCR emits it),
making our review queue immediately useful for the actual workload. This also gives §0's
uncertainty column its first real producer for free.

### 2.5 Video: `tracks.ts` is a model without a UI

`frontend/microfrontends/annotator/src/lib/viewer/tracks.ts` implements keyframes-by-group,
`boxAt()` linear interpolation, `isKeyframe()`, `newTrackId()` — unit-tested, and **zero non-test
consumers** (grep this pass). There is no timeline/keyframe rail, no interpolated-box rendering,
no propagation. XAL's ladder, for sequencing our own: (a) tracker-assisted ids
(ByteTrack/BoT-SORT/TrackTrack → `group_id`, `XAL/anylabeling/services/auto_labeling/trackers/`),
(b) keep-prev carry-forward, (c) SAM2/SAM3 video mask propagation with SSE progress + cancel
(`XAL-S/app/api/video.py:247` — the streaming/cancel pattern is worth copying whenever we get
here). Our step 1 is purely frontend: render what `tracks.ts` already computes.

### 2.6 Server-driven assist widgets

XAL-S's `/v1/models` metadata names the widgets each model needs (`button_add_point`,
`edit_conf`, `add_neg_rect`, task combos…) and the client renders accordingly. Our
`/api/assist/producers` already returns `configured`/`returns[]`/`compatible`
(`assist.py:148-204`) — growing it a `widgets[]` field is the difference between hardcoding the
assist bar per producer and adding the next producer with zero frontend changes. Do this when
producer #3 goes live, not before (premature for two producers).

---

## 3. Active learning

### 3.1 The signal (see §0)

Nothing more to add: consumers built, producers absent. Every backend from §2 must emit
`confidence` and `uncertainty` per shape or the queue stays alphabetical-by-accident, which is
exactly the ALS failure mode (§7).

### 3.2 Auto-accept with QA sampling — lift the idea, not the code

ALS's one genuinely working loop feature: predictions above a confidence threshold are
auto-accepted, **except ~1% are randomly routed to human review** as a QA check
(`ALS/src/main.py:358-359` — `random.random() >= qa_rate`, `qa_rate = 0.01`;
`ALS/src/app/window.py:746-761`). Two of their mistakes to avoid: the rate is not UI-exposed, and
entropy plays no part in the gate (pure confidence + coin flip). Ours maps cleanly: a per-task (or
per-ontology-class) threshold that flips qualifying `prediction` rows to `accepted` with
`reviewer='auto'`, holding out a sampled fraction in the queue. Our audit + publish lineage means
auto-accepted rows stay distinguishable downstream — something ALS cannot do.

### 3.3 Sample selection over the unlabeled pool

Nobody in the comparison set actually has this: XAL has none; ALS's four strategies run once at
folder load over an unscored pool where every image ties at the 0.5 default, so the "prioritized"
order is Python's stable sort of alphabetical order (`ALS/src/core/sample_selector.py:41-58,86`;
selection is invoked exactly once, `ALS/src/main.py:217-242`). We are the best positioned to do it
for real: similarity search already ranks by embedding distance
(`frontend/microfrontends/annotator/src/lib/select/similar.ts` — "find 400 more like this", rank +
cosine shown), and bulk-send already turns picks into tasks. The missing piece is
uncertainty-and-diversity **ranking of unlabeled chunks into a suggested next batch** once §0
produces scores — an extension of the existing select plane, not a new subsystem.

### 3.4 Retrain trigger policy

ALS wrote a sound multi-signal policy — gate on ≥N queued samples, then fire on (time elapsed ∨
entropy shift ≥ 0.15 ∨ class imbalance ∨ avg-confidence drop) — and never wired it: `should_retrain()`
is only reached from a read-only stats dialog (`ALS/src/core/retrain_policy.py`;
`ALS/src/app/window.py:912`). The design is worth lifting as the decision layer in front of our
`POST /api/train` (which is deliberately fire-and-track, `services/medallion/src/medallion/api/train.py:1-4`
— a policy sits naturally in front of it, e.g. keyed on published-labels volume per model.
UNVERIFIED: where that policy should live; candidates are the medallion trigger consumer or a
controlplane cron).

### 3.5 Closing the loop

Three pieces, in dependency order:

1. **A real trainer** in the `train_demo_model` seam (`scripts/ray_train_job.py:332` — the D6 seam
   is explicitly designed for a TorchTrainer drop-in; feature pins, lineage, and the crash-safe
   registry commit all survive unchanged).
2. **An eval gate before promotion.** The cautionary tales are both in-comparison: ALS's
   "validation" is a file-size + loads check (`ALS/src/core/model_manager.py` `compare_models`),
   and its real evaluator (`feedback_validator.py`, 311 lines of before/after IoU) is dead code —
   never imported. Ours should be a held-out eval against published gold, gating the registry
   promotion.
3. **Producer resolution from the registry** — the assist runner resolves "latest promoted
   `models$<name>`" so a completed train actually changes what assists. Registry rows already carry
   everything needed (artifact pointers, version = Lance commit, `ray_train_job.py:10-16`).

### 3.6 Converters

`services/annotator/src/annotator/projects/imports.py:3-11` fixes ONE import format (Arrow IPC) and
defers COCO/YOLO/Label-Studio conversion to "a `scripts/` concern" — the right call, but **no such
script exists** (searched this pass). The moment §3.5's fine-tune loop starts, `scripts/` needs
`coco↔arrow` and `yolo↔arrow` (XAL's `LabelConverter`,
`XAL/anylabeling/views/labeling/label_converter.py`, 2,326 lines, is the field guide for the format
edge cases — pose group-pairing, OBB out-of-bounds, mask color mapping).

---

## 6. Implementation conventions — non-negotiable when any finding lands

Every finding above goes through the standard workflow (CLAUDE.md: brainstorm → spec → plan →
subagent-driven-dev with reviews + TDD — no ad-hoc edits), with the skills loaded per plane.
Route table, by finding:

| Work | Skills to load (marketplace) | Project skills |
| --- | --- | --- |
| Assist runner, producers endpoint, jobs plane (§0, §2) | `fastapi`, `writing-python`, `testing-python` | `rask-services-fleet` (gateway rows, ports), `rask-architecture` (where the runner lives), `openfga` (assist auth doors) |
| Ray Serve hosting + real trainer in the D6 seam (§0, §3.5) | `writing-python`, `testing-python`, `hf-cli` / `huggingface-trackio` (model pulls, run tracking) | `rask-htr-pipeline` (GPU packing, Serve replicas — the runner co-resides with `/transcribe`) |
| Engine tools: OBB, magic wand, brush controls, `unionMasks` wiring (§1) | `writing-typescript` | `rask-frontend` (zone gates, vitest) |
| Zone UI: inspector attributes form, assist bar refinement loop, tracks timeline (§1, §2) | `writing-typescript`, `svelte-skills` + the svelte MCP autofixer (Svelte 5 strict, SSR rules), `shadcn-svelte` | `rask-frontend`, `rask-styling` (components live in `@rask/ui`, not the zone) |
| E2E coverage of every new tool/flow (all) | `playwright-cli` | `rask-frontend` § *Develop ONE zone, no cluster* — `make dev-zone ZONE=annotator` is the loop; new flows need seed-driven mocks in the zone's `e2e/` |
| Schema/wire changes: `InsertRow` attributes, dead columns (§1.3) | `writing-python` + `writing-typescript` (both sides of the wire), `testing-python` | `rask-lance-catalog` (annotations table is catalog-governed), `rask-frontend` |
| Converters in `scripts/` (§3.6) | `writing-python`, `testing-python` | `rask-architecture` (scripts contract: no production-state-changing CLIs) |
| Micro-frontend seams if assist widgets go server-driven (§2.6) | `micro-frontends`, `turborepo` | `rask-frontend` |

Two standing rules that bite here specifically: **Svelte 5 strict + SSR** — browser globals stay
inside `onMount`/`$effect`/handlers, every `.svelte` change goes through the svelte MCP autofixer
(CLAUDE.md Conventions); and **the annotator zone has no `/api` dev proxy** — it reaches
`:8101/:8102/:8103` via its own BFF, so new endpoints need BFF routes, not proxy rows
(CLAUDE.md Conventions; `rask-services-fleet`).

---

## 7. Comparison-target caveats — what NOT to copy

Recorded so future passes don't re-litigate, and because the weight given to each system's design
depends on it:

- **ActiveLabelingSystem's core loop is simulated.** The wired "training" orchestrator emits fake
  epochs with `random.uniform` decay and copies the *current* weights to the shadow path so
  "promotion works" (`ALS/src/core/training_orchestrator.py:164-238`, line 221's own comment);
  `ray.init()` is never called (`:101-103` sets a flag and returns); the real Ray `ShadowTrainer`
  is constructed and **`.train.remote()` never invoked** (`ALS/src/core/shadow_trainer.py`;
  single `.remote(` site is the constructor, `training_orchestrator.py:121`). Its entropy is a
  fabricated distribution — winner's confidence spread uniformly over the other classes — i.e. a
  monotonic function of top-1 confidence (`ALS/src/core/entropy.py:31-35,71-79`). Treat every ALS
  design (§3.2, §3.4) as a paper design that happens to ship with a demo, and take the two named
  ideas only.
- **X-AnyLabeling-Server is a sidecar, not a platform**: no persistence, no annotation storage, no
  users (one static key, `==`-compared, default off with `cors_origins: ["*"]`), errors as
  HTTP 200 + `{success:false}`, config keys for rate-limiting/timeouts that are read by nothing
  (`XAL-S/configs/server.yaml` `performance.*` — zero usages). Copy its registry/widget/SSE
  patterns (§0, §2.6, §2.5); build the serving on our own Ray plane.
- **X-AnyLabeling itself is the real benchmark** — the model zoo (~200 configs, 91 dispatcher
  types, `XAL/anylabeling/services/auto_labeling/model_manager.py:422-2306`), the canvas
  (`XAL/anylabeling/views/labeling/widgets/canvas.py`, 5,525 lines), and the converters are all
  genuinely implemented, current (4.0.1 released the day of this pass), and tested. Where this file
  cites an XAL behavior as the reference UX, it was read, not assumed.

---

## 8. The PER-MODALITY diff — what each blob type still lacks (2026-08-08 evening)

The annotator is a general multimodal tool; this section slices the remaining gaps by modality so
each viewer's backlog is legible on its own. "Have" is one line of what landed; "missing" is
ranked, reference tool named where the UX to copy is known. Cross-cutting gaps (runner, templates,
converters, hotkey rebinding) live in the main table and are not repeated per modality.

### 8a. IMAGE (`ImageViewer` — Pixi/WebGPU canvas)

Have: rect · polygon · point · line · pencil/baseline · magnetic corner-snap · true raster brush
(radius/output/eraser exposed) · lasso multi-select · heatmap coloring · image adjustments ·
relations · per-group layers · uncertainty-ranked review.

Missing, ranked:
1. **Rotated box (OBB)** — schema-complete (`rotation` commit type + column), tool-absent. XAL's
   drag rotation handle + coarse/fine key steps is the reference. Seals, marginalia, skewed blocks.
2. **Per-shape attributes editing** — ontology declares typed attrs; the canvas wire (`InsertRow`)
   and inspector still cannot carry/edit them (finding 4).
3. **Mask boolean ops** — `unionMasks` still has zero callers; brush `semantic` mode settable,
   never read. Cross-label pixel-exclusivity remains "a planned follow-up" in the file's own words.
4. **Magic wand / flood fill** — XAL 4.0.1's tolerance-drag gesture; the cheap non-ML region tool
   for clean scan backgrounds (live-wire was deliberately removed; this is its replacement).
5. **Quadrilateral** (4 explicit corners → canonical polygon; what warped lines/seals and
   PPOCR-style spotting emit) · circle · cuboid · keypoint SKELETONS (pose graphs w/ flip pairs).
6. **Copy/paste/duplicate** (within + across items; system clipboard) — nothing exists.
7. Editing ergonomics: vertex eraser (Alt-drag), wheel rect scale/nudge, per-shape lock + z-order +
   hide-selected (per-group eye only today), shape converter beyond rect→polygon (XAL: 15 pairs).
8. Navigation: minimap/navigator, loop-through-objects (pairs with the uncertainty queue),
   crosshair + cursor readout, compare view (IR/RGB, before/after), 16-bit grayscale support.
9. **Image-level classification template** — `tag` rows exist as data; no dedicated one-image
   classification surface (XAL's Image Classifier; LS choices template).
10. Assist-side: SAHI-style tiled inference for wall-sized scans; segment-everything (AMG).

### 8b. VIDEO (`VideoViewer` — Pixi frame overlay + waveform lane + transport)

Have: exact-frame annotation on paused frames · full spatial toolset · object tracks (keyframes +
linear interpolation + lifetime absence + `+ keyframe`) · soundtrack waveform lane with segment
drag (flat-timeline fallback) · one shared clock · frame stepping/rate.

Missing, ranked:
1. **Track timeline rail** — per-track rows with keyframe diamonds, click-to-seek, drag a diamond
   to retime (CVAT's core video surface). Interpolation renders; its editing surface doesn't exist.
2. **Real fps** — the transport steps on a stated 25 fps ESTIMATE; `requestVideoFrameCallback`
   sampling during the first play would replace the guess with the measured rate.
3. **Tracker-assisted ids** — no ByteTrack/BoT-SORT-style assist assigning `group` across frames
   (XAL wires 13 tracking configs); today every track is hand-promoted.
4. **Video mask propagation** — SAM2/SAM3-video masklets (prompt on one frame, propagate, SSE
   progress + cancel — XAL-S's `/v1/video/*` session contract is the wire reference). Runner-gated.
5. **Track interpolation is bbox-only** — polygon/mask keyframes render at authored geometry only.
6. Clip-level classification + event timeline template (XAL's Video Classifier: I/O marking,
   per-label digit keys, AI auto-segmentation of a clip into events).
7. Seek-bar thumbnails; frame cache for smooth scrub (10 fps snapshot pump today); soundtrack
   spectrogram; per-channel waveforms.

### 8c. AUDIO (`AudioViewer` — waveform canvas)

Have: waveform as the canvas · drag-to-create/resize/click segments · px/s zoom · rate · segment
text/label editing via inspector · same rows/Save as everything else.

Missing, ranked:
1. **Spectrogram view** — the surface half of speech/bioacoustics work; wavesurfer ships a
   spectrogram plugin, so this is wiring + a toggle, not research.
2. **ASR-assisted pre-annotation** — no audio producer exists at all (the registry is vision+text);
   a whisper-family producer emitting segments+transcripts as `prediction` rows is the audio twin
   of HTR pre-annotation (finding 2).
3. **Tiers** — one region lane today; overlapping speakers/phenomena want ELAN-style stacked tiers
   (speaker A / speaker B / noise), i.e. a `group`-keyed lane split, data model already sufficient.
4. Silence-detection auto-segmentation (energy threshold → proposed segments); A-B loop playback;
   waveform minimap for long recordings.
5. Segment-level classification template (sound-event tagging with digit keys — the audio twin of
   8a.9/8b.6).

### 8d. TEXT (`TextViewer` — the document as the canvas; `TextSpanSurface`; `DocumentTextLane`)

Have: document-as-canvas (no pixel medium) · spans as class-tinted highlights w/ overlap stacking ·
select→digit/click labeling · legend-as-keymap · per-line selection sync · lane composition under
image/video · span remove/pick · offsets validated server-side.

Missing, ranked:
1. **Span offset REMAPPING on text edit** — correcting a transcription through the inspector's
   input does not shift existing span offsets, so an early edit silently mis-points every later
   span. Doccano forbids doc editing for this reason; we allow it (HTR correction is a first-class
   act), so we must remap (splice arithmetic on `char_start/char_end`, refusing only on overlap
   with the edited range). The sharpest text defect in the current build.
2. **Span↔span relation ARCS in the text surface** — relations exist and draw between SHAPES on
   the pixel canvas; two spans in the text canvas cannot be linked there (no arc rendering, no
   click-click gesture on marks). LS relations-in-text is the reference.
3. **Text pre-annotation producers** — nothing proposes spans: no NER model producer, and no
   cheap gazetteer/regex assist (a lexicon of parish/person names would pre-mark most of a page).
4. **Document/text classification template** — doc-level classes with digit keys (doccano's
   classification project; Argilla's label questions). `tag` rows exist; the surface doesn't.
5. **Long-document behaviour** — the canvas renders all lines eagerly; a 10k-line document wants
   virtualization + a minimap/outline. Fine at page scale, unproven at book scale.
6. **>9 classes** — digit hotkeys cap at 9; needs class paging or two-key chords.
7. Argilla-class review templates — ranking/preference/chat-eval (RLHF-style response comparison)
   are a different question type entirely; nothing in the estate models "compare two candidates".
8. Entity linking/normalization — a span → knowledge-base id field (LS taxonomy/lookup); relevant
   the moment indexing wants "which Gustav".
9. Text export formats — CoNLL/spaCy JSONL/W3C annotations for the span plane (the text half of
   finding 7's converters).

### 8e. QUESTION TYPES & COMPARISON SURFACES (cross-modal — the Label-Studio/Argilla family)

The family of "ask a question about the item" tasks, orthogonal to modality. The references split
cleanly: **CVAT does not have this family at all** (its classification story is image tags +
per-shape radio/select/checkbox attributes; no pairwise, ranking or rating). **Label Studio** does
it entirely through config templates (`<Choices>` single/multiple, `<Rating>`, `<Ranker>`,
`<Pairwise>`; its LLM-eval templates compose prompt + N responses side by side with per-response
ratings + an overall preference). **Argilla** is the purpose-built reference — label/multi-label,
rating, ranking and pairwise questions are its whole product (the RLHF/DPO collection shapes).

Where rask stands, per question type:

1. **Classification (multiclass/multilabel)** — ~~surface ABSENT~~ LANDED: `ClassificationBar`
   renders the ontology's `tag` classes as a chip bar over any modality's workspace; a chip is a
   `tag` row on the ordinary machinery. (Single-choice enforcement stays with the ontology
   validators at submit; per-shape enum attributes still never reach the canvas — finding 4.)
2. **Rating** — the DATA plane landed with §9.3's attribute editor: a class declaring
   `{name:'rating', type:'int'}` (or an enum 1–5) is now enterable per row and validated at
   submit. Still missing the SURFACE: a star/segment control and an item-level (not per-shape)
   question placement.
3. **Ranking** — nothing models "order N candidates".
4. **Pairwise / preference (A/B)** — ~~nothing models it~~ LANDED as predicted: the compare view
   IS the side-by-side re-composition of the multi-key item plus the choice question
   (`?view=compare` — a pick is a `tag` row on the winning unit, version-guarded per pane).
   Consensus adjudication still compares ANNOTATORS, not candidates — unchanged.
5. **Compare view ("seeing 2")** — LANDED for candidates of one item (see 4: image frames or
   document text side by side). Still open in the other two senses: version history diffs counts
   not pixels, and there is no split view of two LAYERS of the same unit (IR/RGB, before/after).

All five are finding 8 in miniature: none is a new modality — each is a new QUESTION over existing
media, which is exactly what a template system expresses and hardcoded viewers cannot.

---

## 9. THE SAVE-PATH AUDIT (2026-08-08 late) — what each task's saved rows actually are

The e2e suites stub the save POST, so a field the client sends and the server silently drops is
invisible to them: canvas correct, payload asserted, row lands wrong. `tests/unit/
test_task_shapes_saved.py` closes the hole — it drives the endpoint's own primitives
(`NewAnnotation` validation → `new_rows` → real Lance `merge_insert`) per task shape and reads the
rows back, plus a drift GATE: every `InsertRow` wire field must be declared on `NewAnnotation`,
because pydantic ignores unknown fields — an undeclared field is not a 422 anywhere, it is a
column that quietly lands null.

**Found and FIXED by that gate:** text spans did not survive the save. `parent_id`/`char_start`/
`char_end` were in the table schema, in `InsertRow`, written by the controller — and absent from
`NewAnnotation`, so every real save landed them null: the span rendered from the client overlay,
saved, and reloaded as an orphan. Now declared (sentinels `''`/`-1` matching `makeInsertRow`) and
pinned by round-trip tests, composite item included (region + transcription + span + tag in one
version).

**Verified working (the interplay is real):** the assist route normalizes producer dialects
(`rectangle`→`bbox` etc.) BEFORE `_within_contract` filters against the task's derived tool
union, and drops are reported, not silent — proven against a composite polygon/bbox/text
ontology. Save-path identity stamping, prediction scores, tag rows, transcription edits
(`EDITABLE_FIELDS`), OCC base-version — all round-trip.

**Found and OPEN, ranked (the assist-first defect list):**

| # | Defect | Where |
| --- | --- | --- |
| 9.1 | ~~Links wipe on reopen~~ **FIXED**: the shell reads the task draft once at open and `restoreLinks` puts the snapshot back into state and onto the canvas (no-clobber: links drawn before the async read resolved win). E2e pins the loop at the wire — the reopened inspector lists the link AND the next draft PUT carries it, not `[]`. | `AnnotatorShell.svelte`, `annotator.svelte.ts` |
| 9.2 | ~~Spans + links never publish~~ **FIXED**: the published schema grew the textual facet (`parent_id`/`char_start`/`char_end`) and a `links` JSON column (`[{"name","to"}]`, outgoing edges per row, sorted for replay determinism, `[]` never `""`); `_row` emits both from the draft. 34 → 38 columns, pin updated. | `projects/publish.py` |
| 9.3 | ~~`reading-order` preset is unsatisfiable~~ **FIXED**: per-row attributes ride the `metadata` JSON column (declared on both wire models + `EDITABLE_FIELDS`); the inspector grows an ontology-driven attribute editor (int/enum/bool/free, required marked); the draft snapshot maps them onto `Shape.attributes` so submit validation judges what was entered; a declared `order` beats `y` in the transcription lane. Rating-as-typed-int (§8e.2) falls out for free. | `AnnotationDetail`, `annotations/schema.py`, `draft-shapes.ts` |
| 9.4 | ~~SAM click is unreachable~~ **FIXED**: the tool commits everything (clicks included, as zero-size rects) and the accidental-click policy moved to the controller's commit seam, where the meaning is known — an armed producer honours the click as a region prompt (the backend grows it into a patch), an unarmed canvas discards sub-5×5 rects exactly as before. "Click or drag to segment" is now both halves true. | `RectTool.ts`, `annotator.svelte.ts` |
| 9.5 | ~~Two producer registries disagree~~ **FIXED**: `_RETURNS` knows the full family (htr→text, insid3→mask, vlm-judge/embed-propagate→verdicts, honestly empty), so `/assist/producers` lists all six, compatibility computes for the batch producers, and a new `interactive` flag lets the assist bar gate batch-only families out of its mode buttons (they stay in the registry panel as facts). | `assist.py` |
| 9.6 | ~~Jobs op vocabulary mismatch~~ **FIXED**: the wire picklist is exactly the service's `predict|propagate|judge`, and `apply()` returns an honest `unsupported` for a batch `set`/`verdict` (bulk field-writes are the tags plane) instead of posting a guaranteed 422. | `jobs.remote.ts`, `annotator.svelte.ts` |
| 9.7 | **`model_version` has no writer** — every model prediction is unattributable to a model version. | `AssistShape`, `NewAnnotation` |
| 9.8 | ~~No task→text-canvas path~~ **FIXED**: `MediaKind` (models + ontology) accepts `text`, and `taskCanvasHref` emits the task's modality as `kind=` (audio/video/text; image and unknowns fall back to the default canvas). `TaskQueue.canvasHref` now DELEGATES to the one href builder — the second spelling had already drifted. | `task-stream.ts`, `models.py` |
| 9.9 | **Relation model is thin.** `directed` is declared and never read; no cardinality beyond per-relation `required`; rail lives only in the inspector. | `ontology.py:121` |

Also verified: interactive assist is strictly ONE-SHOT (no session, no point arrays, no
encode/decode split despite `sam-click`'s description); the review queue's ranking is exactly
predictions-first + uncertainty-desc, client-side, per-unit; batch jobs are a deterministic mock
that never runs; bulk tags (`TagBatch`/`tag_rows`) are idempotent insert-only rows with arity
checks (covered by `test_annotate.py`).

---

## Method note

Four source-level inventory passes (one per codebase) + direct re-verification of every
load-bearing rask claim in this file (tool union, commit types, mock routing, uncertainty
consumers, dead columns' writers, `unionMasks`/`setImageAdjustments`/`tracks.ts` caller counts,
gateway train row, `train_demo_model`). Line numbers in comparison repos are against the pinned
heads and will drift with their velocity — XAL cut three releases in the week before this pass.
