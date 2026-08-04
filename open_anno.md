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

## #27 — Canvas tool placement (shape DECIDED, not yet scheduled)

The rail's **position is right** — a 44px left vertical strip is the CVAT-shaped answer and it
already is one. What is wrong is **what is in it and in what order**: the tools need grouping
separators and a deliberate order (navigate · draw · assist), rather than one flat list.

Not: floating it over the canvas, and not moving it to the right.

---

---

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
