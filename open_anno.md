# open_anno — the annotation plane's remaining work

Working plan, not settled architecture. Delete this file when the work lands; it does not belong
in `docs/`, which asserts "settled" by location regardless of what the contents say.

**MVP first.** Every item below is ordered simplest-first within itself. The rule for the whole
file: make the core loop *work* before making any part of it rich.

---

## #40 — Campaign operations

All four are wanted. Ordered by cost, cheapest first, because each earlier one makes the next
easier to judge.

### 40a · Queue filter — **LANDED**
Filter the task queue by **state**, **assignee** and **label**. Pure client work over data the
listing already returns (`TaskListing.details`); no new route, no new permission.

Why first: a 1000-item project is unnavigable today, which makes every other campaign feature hard
to even demonstrate. Cheapest thing that makes the rest testable.

Done: state + assignee compose (AND), the state dropdown carries COUNTS so it summarises where the
work is sitting without applying a filter, the empty result says "No items match this filter" rather
than the "no items yet" that would be a lie, and changing a filter CLEARS the selection — TanStack
keys `rowSelection` by id and it survives a row leaving the visible set, so without that a manager
could bulk-accept rows they never saw.

Filtered at the INPUT array rather than through TanStack's column filters: `assignee` is not an
accessor column, and adding a hidden one would be plumbing for the framework rather than the problem.
Pagination, sorting and selection all follow from the filtered set for free.

Side effect worth keeping: `@rask/ui`'s `Select` now passes `onValueChange` through to Bits UI's
`Select.Root`, which always had it. Without it a consumer had to watch `value` from an `$effect` and
assign state there — the Svelte 5 anti-pattern.

### 40b · Bulk assign — **LANDED**
Select N queued items → assign all to one annotator in one action. Extends the existing per-row
assign dialog (`TaskQueue.svelte`, `canAssign`).

Settled as predicted: ONE gated event per item, reported per item — the bulk-accept precedent, and
the only shape the actor model can honour. There is no transaction across task actors, so a rollback
would be a second best-effort loop that can itself half-fail; claiming an atomicity we cannot deliver
is worse than reporting the truth.

Done: the button offers only rows whose OWN `legal_events` carry an `assign` edge (not a second guess
at the machine here), a partial failure reads "1 of 2 assigned to gina — <the server's words>", and a
separate dialog from the per-row one because the two differ in what they act on, what they say and
what they do on submit.

### 40c · Per-annotator metrics
Throughput and accept-rate per person. Read-only, derived from the `Transition` list already
recorded on each task — **no new state**, and that constraint is the point: a metric stored
separately from the transitions it summarises is a metric that can disagree with them.

Done when: the numbers reconcile against the raw transitions for a seeded project, and a person
with zero items shows as zero rather than being absent (absence reads as a bug).

### 40d · Membership UI — *do last*
See and edit who is member / reviewer / manager on a project. This writes **FGA tuples**, so it
touches the authorization model — use the `openfga` skill, and the write path is the lakehouse's
existing tuple write (`access.remote.ts`), which is the estate's reference for gated mutations.

Heaviest and last because it is the only one that can lock someone out of their own project.

Done when: a manager cannot remove their own last manager grant (self-lockout is refused, named),
and every write is `can_manage`-gated server-side.

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

## #41 — Relations + text-span (partly landed)

**Landed:** `RelationClass` in the ontology, `Draft.links` storage, submit-time validation, and the
two-click link editor in the annotation inspector (arming adopts the inspected shape; the target is
picked on the canvas).

**Also landed:** the link is DRAWN on the canvas — `ArrowDataPlugin.setLinks()` takes row indices
and strokes a line plus a directed arrowhead into a `links` Graphics on the annotations container,
so it inherits the viewport transform. The drawing MATH is `linkPath()`, extracted so an arrowhead
pointing the wrong way is catchable without a GPU.

**Remaining: text-span annotation — DESIGN DECIDED, NOT BUILT.**

`text` is in the shape vocabulary and nothing can produce one. Four things are missing, and I
checked each rather than assuming:

| Needed | State today |
| --- | --- |
| A surface to select text on | `viewerFor()` has Image, Audio, Video. There is no text viewer. |
| A source of text for a unit | The annotator serves `chunk-frame` (image) and `annotations` (Arrow). No endpoint returns a unit's transcription. |
| Offsets on a shape | `Shape` carries x/y/w/h/rotation/polygon/t_start/t_end. No character range. |
| Offsets in the Arrow schema | `annotations/schema.py` has a `text` STRING column, no start/end. |

**The decision, taken rather than left open.** Two models were possible:

* **(a) A span INTO an annotation's own `text`.** Every row already carries the transcription of its
  own line or region, so the text is present client-side already. A span is a child annotation with
  a character range into its parent's text. No new endpoint, no new viewer — the inspector already
  renders that text field.
* **(b) A span over a whole-unit transcription.** Needs a text endpoint, a text viewer, and a
  document-level coordinate space that nothing else in this plane has.

**Take (a).** It is the one the codebase already supports: the Arrow table is per-unit annotation
rows each carrying `text`, and (b) would invent a second coordinate space beside the pixel one for a
capability the medallion's HTR output does not currently produce in document order.

**The work (a) implies**, in order:
1. `Shape.char_start` / `char_end: int | None`, and a `parent_id` naming the row the span is inside.
2. The same three columns in `annotations/schema.py` — a THIRD facet beside the spatial and temporal
   ones the schema already comments as such.
3. Validation: a span's range must lie inside its parent's `text`; a span whose parent is gone is
   the same class of orphan as a link to a deleted shape and must be refused the same way.
4. Selection UI on the inspector's existing text field, committing through the same undo stack.

**Why it is not built here.** Step 2 changes the schema of the PUBLISHED table, which is the
contract the gold tier and every downstream reader see. That is a migration with a real blast
radius, and doing it without being able to verify the publish end-to-end would be the kind of
half-landed change this file exists to prevent. It is the one item in this wave I am stopping short
of rather than starting and leaving mid-flight.

**Deliberately not claimed:** that the right PIXELS are lit. A WebGL drawing buffer is not preserved
after present, so a screenshot of it proves nothing (this zone's delete spec records the same). What
is pinned is the draw CALL (relations.test.ts) and the draw MATH (link-path.test.ts), each verified
failing when broken, plus an e2e that fails if the draw path throws in a real browser.

---

## #37 — Stale items (partly landed)

**Landed:** removal (`DELETE /projects/{id}/tasks/{task_id}`, `can_manage`, refused past
`labeling`), and the read path now reports the server's actual reason — `<img>.onerror` carries
neither status nor body, so a clear `404 dataset 'demo' not found` used to reach the user as
"Failed to load image: <url>".

**Remaining:** the WRITE half. `POST /projects/{id}/items` does not verify that each item's dataset
resolves, so a stale item can still be created. Refusing at send is what stops the trap being set
in the first place; removal is only the escape hatch.

---

## #27 — Canvas tool placement (shape DECIDED, not yet scheduled)

The rail's **position is right** — a 44px left vertical strip is the CVAT-shaped answer and it
already is one. What is wrong is **what is in it and in what order**: the tools need grouping
separators and a deliberate order (navigate · draw · assist), rather than one flat list.

Not: floating it over the canvas, and not moving it to the right.

---

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
