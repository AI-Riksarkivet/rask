# open-bulk-active — an excel-grade AI-assisted bulk-labeling surface, learned from HF aisheets

Working spec, **2026-08-09**, produced by a 7-agent source-level analysis workflow over
**huggingface/aisheets `cadf5cd`** (Qwik + DuckDB/SQLite/LanceDB, Apache-2.0), cross-checked by an
adversarial critic agent. Unsettled work; this file is deleted when the design lands or is
rejected. Companion to `open_anno_active.md` (the canvas/assist plane); this file is the GRID
plane: bulk labeling as a modern spreadsheet over the corpus, columns driven by LLM/VLM
producers, filtering and embeddings as the selection machinery.

**Evidence convention**: `path:line` claims are read from aisheets source at the pinned head
(prefix implied); rask claims are repo-relative. `UNVERIFIED` named inline. The critic's
verified corrections are folded in; its **unread list** is reproduced in §7 — those subsystems
are scope-cuts of this analysis, not certified absent.

**Why aisheets**: it is the strongest existing expression of the owner's ask — "labeling in
excel for bulk labeling, combined with AI endpoints, filters, and embeddings." It gets the UX
loop right and the infrastructure wrong; rask's estate (Lance/Arrow, the producers registry,
the guided-generation contract, the jobs seam, OpenFGA) is precisely the infrastructure it
lacks. Steal the loop, keep our substrate.

---

## §1 What aisheets IS (the product loop, verbatim from source)

One prompt box ("Write your dataset description here") or a file/Hub import → a dataset of rows
in a virtualized grid (`routes/home/index.tsx:29-43`). Every AI capability is a **column**: a
column carries a persisted **Process** — `{prompt, modelName, modelProvider?, endpointUrl?,
columnsReferences, searchEnabled, imageColumnId?, task, updatedAt}` (`state/columns.ts:13-31`).
Prompts are mustache templates referencing sibling columns as `{{column_name}}` with
cursor-anchored autocomplete; `columnsReferences` is DERIVED by scanning the template, never
hand-picked (`template-textarea.tsx:146-185`). Four task types gate the model picker:
text-generation, image-text-to-text (VLM over an `imageColumnId`), text-to-image,
image-to-image (`columns.ts:7-11`). Generation streams cell-by-cell (claim → value/error) in
`Promise.race` windows of `NUM_CONCURRENT_REQUESTS`; **drag-to-fill is the generation gesture**
(selection → `{offset, limit}` → run the column's process over exactly those rows,
`table-body.tsx:190-210`). Editing a cell marks it **validated**: validated cells are never
overwritten by regeneration and are auto-injected as few-shot examples into every subsequent
run of that column (`generate-cells.ts:263-266`, `collect-examples.ts:10-48`). Optional web
grounding: LLM-written queries → Serper → scrape → markdown chunks → LanceDB embeddings →
hybrid FTS+vector retrieval with RRF feeding a sources section, with per-cell url+snippet
citations (`websearch/embed/engine.ts:280-305`). Export = DuckDB `COPY … FORMAT PARQUET` →
one-click push to the HF Hub.

## §2 Facts that shape the design (condensed; the full 133-fact set lives in the workflow output)

- **Cell model**: `{id?, idx, generating, validated, value, error, sources[], updatedAt}` —
  lifecycle and provenance are per-cell, rendered as layered skeleton/error/thumbs-up states
  (`state/columns.ts:74-92`). Metadata is a SPARSE overlay (SQLite rows only for cells with
  state) merged at read time over the DuckDB values (`repository/cells.ts:222-293`).
- **Preview-first economics**: autodataset populates 5 rows per column, full runs are an
  explicit expansion (`run-autodataset.ts:451-465`).
- **Cancellable with cleanup**: AbortController on the process; onabort re-fetches in-flight
  cells and clears `generating` flags so nothing sticks spinning (`useGenerateColumn.ts:22-47`).
- **NO structured output anywhere** — format compliance is begged for in prose and parsed by
  regex; the critic verified this holds even in the offline vLLM scripts
  (`materialize-prompt.ts:82,129`).
- **No row filtering or sorting exists at all** — the only WHERE is the paging window
  (`list-table-rows.ts:23-35`). The "excel experience" is half-built; embeddings never touch
  dataset rows, only scraped web pages.
- **No job system**: generation lives inside the HTTP request as a `server$` async generator; a
  dropped connection loses the run, no resume, no server-side run record.
- **Critic-verified bugs worth knowing**: the paging "window cache" actually keeps only the
  newest window (`table-body.tsx:231-244`); server clamps row-count by MIN of inputs while the
  UI advertises MAX; the export metadata builder discards its own reduce result.
- **The offline twin**: `scripts/extend_dataset/with_vllm.py` builds a validated column
  dependency DAG from the same `columnsReferences` vocabulary, topo-sorts, and batch-generates
  with local vLLM — aisheets' own proof that ONE recipe format can execute both interactively
  and as a batch job (critic's unread-list find).
- **Attribution hole** (critic): `validated` is a bare boolean — no `validatedBy`/`validatedAt`.
  Unacceptable for an archive; rask's overlay must carry attribution from day one.

## §3 What we steal, ranked (the critic's consensus top-5, then the rest)

1. **The validated-cells flywheel, with lock-on-validate.** A human-accepted cell is (a) frozen
   against regeneration and (b) auto-promoted into the few-shot examples of every subsequent
   run of that column. In rask this is STRICTLY better than in aisheets: validated rows are
   schema-conformant by construction (the `generation_schema` guided_json contract), they
   double as `insid3`/`embed-propagate` exemplars, and they carry `validatedBy/validatedAt`
   (fixing the attribution hole). This unifies "human correction teaches the machine" across
   the grid and canvas planes.
2. **Column-as-recipe, upgraded with the offline DAG.** Every AI column persists its recipe:
   `{producer, prompt template, columnsReferences (derived from {{refs}}), task,
   output_contract (a JSON-Schema fragment), updatedAt}`. One recipe format, two executors:
   interactive preview (N≤5 rows, assist plane) and the jobs seam (topo-sorted over column
   dependencies, vLLM batch — the `with_vllm.py` shape on our Ray/lance stack).
3. **Per-cell lifecycle streaming.** Claim events (`generating: true`) paint skeletons for the
   whole range instantly; results fill as-completed via bounded concurrency; abort walks
   in-flight cells and clears flags. In rask: server-side on the jobs seam with SSE/Arrow-IPC
   deltas — never tab-lifetime (aisheets' stranded-run failure mode).
4. **The adoption gestures: drag-to-fill + optimistic preview column.** Drag the fill handle →
   run the recipe over exactly those rows; adding a column inserts an optimistic placeholder
   and auto-runs the first 5 rows so output is visible in seconds. Plus the force-open
   onboarding tooltip that makes the invisible gesture discoverable.
5. **Sparse cell-metadata overlay over immutable columnar values.** `{validated, validatedBy,
   validatedAt, generating, error, provenance}` materialized only for cells that have state,
   merged per fetch window — maps directly onto Lance (a small upsertable side table over the
   immutable dataset version) and is the substrate steals 1–4 depend on. Extend provenance to
   `{producer, model+revision, prompt hash, confidence, job id, retrieval neighbors}`.

Also stolen (smaller): `{{column}}` mustache templating with derived references and
rename-propagation; preview-first economics fused with our filters ("run on this filtered
slice" before "run on all N"); typed progress events driving a run panel; content-hash
response memoization (tweak-prompt-rerun is free for unchanged rows); task-filtered producer
picker (our registry's declared `inputs`/`returns` ARE the filter); `imageColumnId`-style
row-aligned VLM calls (ours: the region crop / page image per item); hybrid FTS+vector+RRF
over Lance as the similarity/filter backbone — aisheets does it in ~10 lines against LanceDB
and our annotations already ARE Lance; content-negotiated slice export (parquet/Arrow of the
filtered+labeled view); cancellation with guaranteed UI cleanup.

## §4 What we deliberately do NOT copy

- **Regex-parsed LLM plans** (`processTextConfigResponse`) — our `generation_schema` +
  guided_json replaces every free-text parse, including the "describe a task → get columns"
  bootstrap.
- **Request-lifetime generation** — bulk runs belong to the jobs seam with a run record,
  resume, and lineage; never to an HTTP connection.
- **The O(n²) from-scratch loop** (every previous output re-sent per row) and prompt-begged
  dedup.
- **Three storage engines** (SQLite + DuckDB + LanceDB) with values duplicated between two of
  them — rask keeps ONE substrate (Lance/Arrow) for rows, labels, state overlay and embeddings.
- **Cookie-UUID anonymous auth and WHERE-clause tenancy** — OpenFGA stays.
- **In-place overwrite with no history** — Lance versions + OpenLineage per run give diffable,
  undoable bulk runs for free.
- **Module-load global state** (top-level awaited LanceDB/pipeline singletons), the 965-line
  form component, stringly-typed event switches, UA-sniffed browser branches.
- **Untyped cells** — `Cell.value?: any` (`src/state/columns.ts:85`) means no layer knows what
  a cell should hold; "types" exist only as a cosmetic string (`create-table-column.ts` maps
  everything to DuckDB `TEXT` except image→`BLOB`). Ours stay contract-typed at the boundary
  (ontology attr type → submit validation → guided_json) even while the UX defers asking.
- **Refusal text stored as data** — the prompt *instructs* the model to answer "No more items"
  when it runs dry (`materialize-prompt.ts`), and that string lands in cells like any value.
  guided_json makes this unrepresentable for us: a constrained column cannot receive prose.

## §5 The open-bulk design (rask mapping)

**Surface**: a `/bulk` area in the annotator zone (nav exists). The grid is a virtualized table
over a **selection** — a labeling task's items, or a filtered corpus slice (the same
`Selection {level, keys, where}` the jobs seam already speaks). Rows are items; columns are:
identity/media thumbnail, corpus facets, then **ontology-derived columns** (one per label
facet: classification tag, transcription, attributes) and **derived producer columns** (each
carrying a recipe). The Arrow row model is already ours; TanStack virtual on the client.

**Columns speak the ontology.** A tag column's cells are constrained to declared classes; an
attribute column to its enum/int/bool type; a transcription column is free text — all derived
from `LabelOntology`, and every producer column's `output_contract` is a fragment of the same
`generation_schema` that already rides `output_schema` to vLLM. Filling a cell = writing an
annotation row / metadata patch through the EXISTING save wire (edits/inserts, base_version
OCC) — the grid is a VIEW over the same table the canvas edits, never a second store.

**The dynamism bar (first-hand audit, 2026-08-09).** What "dynamic like the reference" means
concretely, each fact read in source, with the design consequence it forces on us:

1. **One sentence → a living column.** Every column header carries a ✨ button opening a bare
   textarea ("Type your action, e.g. translate to French"); Cmd+Enter creates the column
   *immediately* — auto-named `column_N` (`execution.tsx:53-62`), the source column
   auto-referenced, the sentence becoming the prompt (`add-column-placeholder.tsx:128-213`).
2. **Generation auto-starts.** `mode === 'add'` fires the first run ~500 ms after the column
   exists (`execution-form.tsx:570-581`). There is no "configure, then run" — the column is
   filling before the form has been read.
3. **Type is inferred, never asked.** A new column is `type: 'unknown'` until the first
   generate, then takes the model's `supportedType` (`execution-form.tsx:546-548`); template
   choices carry their own type map (detect-objects→text, colorize→image).
4. **References are implicit.** `{{column}}` mentions in the prompt ARE the dependency edges;
   the column you launched from is wired in automatically.
5. **Everything edits live.** Prompt/model re-editable and re-runnable at any time (re-runs
   skip validated cells — `generate-cells.ts:263-266`); rename is click-header-and-type;
   cells stream in one by one via an async iterator and a run is abortable mid-flight with
   partial results kept (`useGenerateColumn.ts:32-67`).
6. **Editing a cell IS validating it.** Typing in a cell and thumbs-up go through the same
   `validateCell` wire (`cell-actions.tsx:88-90`, `validate-cell.usecase.ts`); the corrected
   value immediately joins the example pool.
7. **A whole dataset from one sentence** (autodataset): an LLM proposes name + columns +
   prompts, which are materialized and run — the column DAG bootstraps itself
   (`run-autodataset.ts`).

**Design consequence — act-first, declare-derived.** Our earlier framing ("＋ column = fill a
name + type declaration") set the ceremony in the wrong place. The declaration stays — it is
what buys validation and guided decoding — but it is **derived from the action, not demanded
before it**: "＋ column" is ONE textarea; Enter creates the ontology declaration silently
(auto-name from the action, `type: free` unless the chosen producer's declared `returns` or a
template implies better), PATCHes the ontology, and starts preview-5 immediately.
**Progressive typing**: a column is born loose (`free`) and hardens as the task matures —
tightening `free → enum(choices)` later retro-validates existing cells and upgrades the
column's guided_json branch. The YAML view shows the derived declaration after the fact for
whoever wants to see or tighten it; nobody is forced through it.

**Runs**: preview (≤5 rows) executes through the assist plane synchronously; full runs submit
one job per column execution with `{recipe, selection, skip: validated}` on the jobs seam,
streaming per-cell claim/result events back (SSE), each cell landing as a `status='prediction'`
row/patch with provenance. Accept-in-grid = the same status flip the review queue does;
validated cells join the recipe's example pool and the exemplar sets.

**Embeddings**: the estate's embed seam indexes item/region embeddings in Lance;
filter-by-similarity ("rows like these"), exemplar-neighborhood selection ("label all
neighbors of these 3"), and dedup/cluster views ride the same hybrid FTS+vector+RRF query
aisheets demonstrated. This is where open-bulk meets `insid3`/`embed-propagate`: the grid is
the natural exemplar picker at corpus scale.

## §6 Implementation plan (phased, each phase shippable)

1. ~~The grid over a selection~~ **LANDED 2026-08-09** (`/bulk?task=<id>`, linked from the task
   detail): one row per item — thumbnail, key, workflow state, assignee, corpus facet,
   then LIVE annotation state (status counts, item tags, transcription excerpt) fetched
   per VISIBLE row (IntersectionObserver + the Arrow wire; `content-visibility` rows), the
   sparse-fetch discipline §3.5 demands. Rows deep-link into the canvas with full context.
   Filtered-slice selections (beyond one task) ride phase 6's selection work. (was: medium)
2. ~~Cell state overlay + inline accept/edit~~ **LANDED 2026-08-09**: ✓-accept flips every
   `prediction` row to `accepted` in one save; ✎ edits the transcription excerpt inline —
   both through the EXISTING save wire (per-field edits + base_version OCC, no second store).
   Attribution needed no new schema: the save path already stamps `reviewer`/`updated_at`
   server-side on every touched row, so the grid re-fetches and renders the server's stamp
   (`✓ gina · time`) rather than claiming identity client-side. A 409 re-fetches; an edit
   conflict keeps the draft open over the fresh state. `summarize()` grew the actionable
   fields (predictionIds, textId, reviewer/reviewedAt). (was: medium)
3. **Recipe columns + preview-first**: act-first add-column (one textarea; declaration
   derived — auto-name, inferred/progressive type, silent ontology PATCH), producer picker
   registry-driven, `{{column}}` templating, auto-run 5 rows through the assist plane,
   drag-to-fill for scoped ranges. (large)
4. **Jobs-seam bulk runs**: recipe → job with skip-validated, SSE cell streaming, cancel with
   cleanup, run record + lineage; content-hash memoization. (large)
5. **The flywheel**: validated cells → few-shot examples in recipes + exemplars for propagate;
   re-run deltas visible via Lance versions. (medium)
6. **Embedding selection**: similarity filter, neighborhood select, cluster view. (large)
7. **Slice export** (parquet/Arrow of the filtered+labeled view) and the guided "describe →
   columns" bootstrap over `generation_schema`. (medium)

## §7 The critic's unread list (honest scope-cuts of this analysis)

`scripts/extend_dataset/` (read only enough to verify the DAG + no-guided-decoding claims);
the raw upload/JSON API routes; the websearch scraper mechanics (explicitly scoped OUT — rask
grounding, if ever, is catalog/archival sources, not scraped web); the auth/OAuth plane beyond
the attribution finding; dataset lifecycle/sidebar; the (thin) test surface — upstream tests
only the DuckDB table layer and hub import, so aisheets' generation semantics are designs to
re-specify, not behaviors to preserve.
