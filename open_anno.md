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

## #43 — The JSON columns are opaque strings (NEW)

Three columns hold JSON and are typed `pa.string()`, so nothing can query them:

| Column | Where |
| --- | --- |
| `attributes` | the PUBLISHED table — its own comment says `# json` |
| `metadata` | the annotations table |
| `links` | the annotations table |

This is the session's recurring shape one more time: the ontology declares per-class attributes with
REAL types (`free` / `int` / `enum` / `bool`), enforces them at submit, publishes them — and then no
consumer can filter on one. Reading every row and parsing client-side is the only option.

Lance types these natively. `pa.json_()` stores JSONB and gives `json_get_string` / `json_get_int` /
`json_get_bool` / `json_extract` / `json_array_contains` in filters, a scalar JSON index on a hot
path (`IndexConfig(index_type="json", parameters={"target_index_type": "btree", "path": "order"})`),
and an INVERTED index for full-text over a whole document
(https://lance.org/guide/json/).

**The caveat that shapes the design:** JSON functions work in FILTERS only, not in projection. You
can select rows where `json_get_int(attributes, 'order') > 3`, but you cannot project
`attributes.order` as a column. So a training consumer that wants a field AS a column still needs a
derived/computed column — the JSON type buys querying, not free flattening.

Scope when picked up: change the three column types, keep the writers' JSON encoding as-is (they
already emit sorted-key JSON strings for byte-identical replays), add an index on whichever path the
review queue actually filters by, and prove a filter returns the right rows. The `attributes` change
touches the published table, which is additive and metadata-only in Lance — the same property that
made the text-span facet safe.

---

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
