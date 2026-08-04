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

## #39 — Import (shape DECIDED)

**Annotation import**, not media import: bring EXISTING labels in so work started elsewhere can be
continued in rask.

**ONE format — the canonical Arrow schema we already have.** Not a parser zoo.

The first version of this section proposed accepting COCO, YOLO and Label Studio JSON directly. That
was wrong, and the owner said so: the estate ALREADY has a canonical annotation format —
`annotations/schema.py`'s `EMPTY_SCHEMA` — and it is the contract the canvas reads, the draft
validates and the publish writes. Three parsers inside the service would be three things that rot,
each with its own edge cases and tests, to produce something the schema already describes.

So the endpoint accepts **Arrow IPC matching the annotations schema**. Converting COCO or YOLO into
it is a `scripts/` concern — the repo's own convention for one-shot tooling — replaceable without
touching the service, and testable on its own.

Notes for whoever builds it:

- **Bytes, not a table reference.** The point of this task is data NOT yet in the lakehouse, so the
  transport is Arrow IPC on a `+server.ts` route, matching the rule the estate already follows for
  bulk payloads. A governed Lance table is a different (easier) case and needs no import at all.
- **The landing target is the task's DRAFT** (`Draft.shapes` + `Draft.links`), not the annotations
  table. An imported label is unreviewed work, and the draft is where unreviewed work lives; it
  reaches the table through the ordinary submit/accept/publish path, so an imported label earns the
  same provenance as a drawn one.
- **`status` is `prediction`, never `accepted`.** Same stance as an assist result: importing is not
  reviewing.
- **The task's captured ontology is the contract.** A label outside the taxonomy must be refused AT
  IMPORT, naming the label — not discovered at submit after someone has reviewed it.
- **Shape types normalise through `@rask/labeling/shape-types`.** Even one format needs this: rows
  written by older tooling carry `rectangle`, a name neither side accepts.

---

---

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

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
