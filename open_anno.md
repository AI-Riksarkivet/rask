# open_anno — the annotation plane's REMAINING work

Working plan, not settled architecture. It does not belong in `docs/`, which asserts "settled" by
location regardless of contents. **Delete this file when the last section below lands.**

## What this file is NOT

Everything the 2026-08-04 wave delivered has been REMOVED from here, because git history is the
record and a plan file that also describes finished work cannot be read as a to-do list. Landed and
gone from this file: **#37** (send refuses an unresolvable dataset; item removal; the read path
reporting the server's real reason), **#41** (relations end to end — ontology, storage, submit-time
validation, the two-click editor, the drawn canvas edge — and text spans: the textual facet,
validation, and the selection editor), and **#40a–40d** (queue filter, bulk assign, per-annotator
metrics, membership).

What is below is what is genuinely NOT built. Two of the three have their design decided; one is
the owner's to design.

---

## #39 — Import — **LANDED**

`POST /tasks/{id}/import` accepts **Arrow IPC matching the annotations schema**, and nothing else.

- **Backend** — `annotator/projects/imports.py` decodes, normalises shape names through the canonical
  vocabulary, and refuses against the task's CAPTURED ontology. 29 tests in
  `tests/unit/test_annotation_import.py`, 12 endpoint tests in `test_task_endpoints.py`.
- **Converter** — `scripts/coco_to_annotations.py` + 11 tests. The proof that the division holds:
  the converter translates and does not judge; the service judges and does not translate.
- **UI** — `ImportButton.svelte` on a CLAIMED task's row, posting bytes to the zone's
  `+server.ts` import route and showing the server's refusal verbatim.

Decisions, all made against the codebase's precedent:

- Lands in the **DRAFT**, so an imported label reaches the table through submit → accept → publish and
  earns the same provenance as a drawn one. The draft has no `status` column (`status` is an
  annotations-table field), so "not drawn here" is `source="import"` plus a new `DraftOrigin` member.
- **`import` is its own origin**, not folded into `model` — imported work may be a person's, made in
  another tool, and calling it "model" is a false provenance claim on every published row.
- **APPENDS**, never replaces. A whole-draft replace matches how `save_draft` stores and would
  silently destroy existing hand-drawn work with no undo anywhere in the actor.
- **Fails CLOSED**, unlike the assist endpoint beside it, which fails open so an unreadable rule never
  loses an interactive prediction someone is watching for. An import is a bulk write nobody is
  watching. One bad label refuses the WHOLE import.
- Only the **membership** half of the ontology contract runs at import (`membership_violation`,
  extracted from `validate_against_ontology` and shared). The completeness half — every required
  class present — is correct at submit and wrong at import, which is partial work by definition.

**Found and fixed on the way in:** the draft endpoint dropped every relation. `SaveDraftRequest`
never declared `links`, and pydantic ignores unknown keys, so the client sent them, the actor was
built to store them, and the endpoint between forwarded shapes alone. Invisible in a live drive —
canvas links live in client state and only vanish on reload.

**Pinned twice, because it bit twice:** `pa.Table.from_pylist` infers its schema from the FIRST row,
so a heterogeneous row list silently drops every column row one happens not to carry. Guidance for
`scripts/` converters: emit the canonical schema explicitly.

## #27 — Canvas tool placement — **LANDED**

The rail's POSITION was never the problem — a 44px left strip is the CVAT-shaped answer and already
was one. Its contents had no order and no grouping: ten buttons in one undifferentiated column,
asking the annotator to remember which of them commits a shape and which only moves the view.

Now three bands in reading order — **navigate · draw · assist** — with the band a property of the
TOOL, so the order is derived rather than hand-arranged. `bandsOf()` lives beside the registry so the
rail and its tests use the SAME function; a test that re-implements the grouping can pass while the
rail disagrees with it.

Two things the work surfaced:

* **`drawing` was doing double duty.** `lasso` carries `drawing: true` — it is an edit-mode
  affordance — while committing no shape at all: it selects. No amount of reordering a flat list
  expresses that; a band does.
* **Empty bands are dropped, separators and all.** The filter already hides a drawing tool the task
  refuses, so a bbox-only task empties `assist` entirely, and a separator rendered for an absent band
  is a hairline with nothing either side. Seen live: with no CV pipeline the assist band and its
  separator are both simply absent.

Hotkeys renumbered to match reading order (1–9, `M` for the one machine-assisted tool). Nothing
pinned them — verified before changing — and `TOOL_KEYS` derives from the registry, so the keymap
followed with no separate edit. Leaving them would have shipped `1,2,7 | 3,4,5,6,8,B | 9`, which is
the same complaint in a different form.

---

---

## #43 — The JSON columns are opaque strings — **LANDED**

`attributes` (published table), `metadata` and `links` (annotations table) were `pa.string()`: valid
JSON in every writer, entirely opaque to every reader. All three are now `pa.json_()` — Lance JSONB —
so `json_get_*` / `json_extract` / `json_exists` work in a filter.

Proven end to end in `tests/unit/test_json_columns.py` (9 tests), against REAL Lance datasets built
from the REAL schemas — an in-memory Arrow test would prove the annotation and nothing about the
engine. Revert-checked: with the columns back on `pa.string()`, **8 of the 9 fail**, the central one
with `No function matches 'json_get_int(Utf8, Utf8)'`.

The IPC hop is covered on purpose: the annotator never writes Lance, it posts Arrow IPC to the
catalog. A type that degraded to string on the wire would leave the published table unfilterable
while every schema-level test still passed.

**Two things the experiments contradicted, recorded because assuming either would have been wrong:**

- **`alter_columns` string→json is a SILENT NO-OP** in lance 9.0.0 — it reports success and the type
  stays `string`. So this applies to NEWLY WRITTEN tables; existing string-typed tables need a
  rewrite, not an alter.
- **An empty string is not refused** — Lance coerces `""` to JSON `null` (stored as `'null'`).
  Malformed JSON (`{oops`) IS refused, at `write_dataset`, as
  `OSError(LanceError(Arrow): Failed to encode JSON)`. Both pinned. No writer emits `""` today
  (`_json_attributes` returns `{}` for a shapeless row), and that is now load-bearing rather than
  tidy: garbage that used to land happily now fails the whole publish write.

**No index was added, and that is a finding rather than a cut.** The plan said to index "whichever
path the review queue actually filters by". There is no such path: the annotations dataset is
filtered ONLY by descriptor identity key fields (`chunk_key_filter` → `doc_key` + int key fields),
and the review queue's filter is CLIENT-side over task actor state (`t.state`, `t.assignee`,
`TaskQueue.svelte:70-77`) — it never touches this Lance table. A Lance JSON index must name one
concrete path, and it must be rebuilt after every overwrite
(`medallion/services/compute.py:200-212`), so indexing a path nobody queries is standing cost for no
query. The type change is precisely what unblocks the index the day a filter lands.

**The caveat that still shapes downstream design:** JSON functions work in FILTERS only, not in
projection. You can select rows where `json_get_int(attributes, 'order') > 3`; you cannot project
`attributes.order` as a column. A training consumer that wants a field AS a column still needs a
derived column — the JSON type buys querying, not free flattening.

## #28 — Explorer: choose a dataset, search across several — **3 of 4 DONE, 1 BLOCKED**

Four parts. Three are settled; multi-dataset search is BLOCKED, and the reason is below rather than a shrug.

**Done — the dataset picker.** The mechanism was never missing: `descriptor-store` has always read
`?dataset=<id>` and the backend has always listed corpora at `GET /api/datasets`. Nothing on screen
said so, so choosing one meant hand-editing the URL. `dataset-picker.svelte` + the pure
`dataset-choice.ts` (11 tests). The URL stays the source of truth; the default corpus drops the param
rather than naming itself; every other param survives. Switching corpora also recomputes the sidebar,
which is the descriptor-driven nav below doing its job.

**Done — the sidebar `Guide` row is gone.** A sidebar is where a zone's AREAS live; documentation is
not an area of the corpus. The `/explorer/guide` PAGE (2091 lines) is untouched and still reachable
by URL — deleting it is a bigger call than "why is Guide a sidebar row".

**BLOCKED — multi-dataset search.** Not a scope cut; a real obstacle plus a real question.

The *transport* is ready: `GET /api/search` already takes an optional `dataset`, so fanning out N
calls is straightforward. What is not ready is the RENDER path. Every hit renderer resolves its
display fields through the module-level `activeView()` singleton — **61 call sites across 27 files**
(`hit-card`, `hit-table`, `doc-tile`, `player-pane`, `transcript-window`, `chunk-timeline`, the atlas,
the workflow nodes, `utils.hitKey`, …). Two corpora have different column names, so a merged result
list cannot be rendered until a per-hit `DatasetView` is threaded through all of them. That is a
substantial refactor of the zone's core, not an afternoon.

And before doing it, one question needs an answer that is not mine to pick: **scores are not
comparable across corpora.** BM25 is normalised per index and vector distances depend on the space,
so "merge by score and interleave" produces a ranking that looks authoritative and means nothing. The
alternatives are materially different products:

- **Grouped per corpus** — N result lists side by side, each internally ranked. Honest, no cross-corpus
  scoring claim, and it needs no per-hit view because each list has ONE view.
- **Interleaved with per-corpus quotas** — one list, k hits from each, ordered within each corpus.
- **True fused ranking** — requires a comparable score (reciprocal-rank fusion is the usual answer)
  and is the only option that needs the full per-hit-view refactor.

**Grouped** is the cheapest by a wide margin and the only one that avoids claiming something false.
Worth confirming before anyone builds the other two.

**Already resolved — the lakehouse `/catalog/projects` route.** The issue asked for it to be
reconsidered; it had already been deleted by the 2026-08-03 IA ruling
(`140315e feat(ia): the project is the top of the hierarchy, and the main menu says so`). VERIFIED
rather than taken on trust: the directory is absent from `src/routes/catalog/`, and
`lakehouse/e2e/lineage/shell.spec.ts:220` asserts the link has count 0.

The reasoning is worth keeping because it is the same shape as the Guide row above: the catalog
hierarchy runs project > warehouse > namespace > table, so listing "projects" as a leaf INSIDE one
project's catalog inverted it — it described lakekeeper's tenant-list endpoint rather than this
product's model. There is ONE project concept and it is the estate's: `/projects` in the home zone.
The lakehouse owns what is BELOW a project, and nothing above it.

---

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
