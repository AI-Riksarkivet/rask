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

## #39 — Import (shape DECIDED, not yet scheduled)

**Annotation import**, not media import. Bring EXISTING labels in — COCO / YOLO / Label Studio
JSON — into an annotation project's tasks and drafts, so work started elsewhere can be continued
in rask.

Not the other three shapes that were considered: media upload, registering an external path, and
a UI over the existing IIIF harvest are all separate pieces of work and none of them is what was
meant here.

Notes for whoever picks it up:
- The landing target is a task's **draft** (`Draft.shapes` + `Draft.links`), not the Lance
  annotations table — an imported label is unreviewed work, and the draft is where unreviewed work
  lives. It reaches the table through the ordinary submit/accept/publish path, so imported labels
  get the same provenance as drawn ones.
- Imported shapes must be normalised through the canonical vocabulary
  (`@rask/labeling/shape-types`) exactly like a model's predictions are. COCO says `bbox`,
  YOLO says a normalised centre-form box, Label Studio says percentages — three dialects, one
  seam, and the seam already exists.
- The task's captured **ontology** is the contract: an import carrying a label outside the
  taxonomy must be refused *at import*, naming the label, not discovered at submit.
- `status` on an imported shape should be `prediction`, not `accepted` — the same stance as an
  assist result. Importing is not reviewing.

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
