# open_browse — `/annotator/browse` as the bulk-labeling surface (#42)

Working plan, not settled architecture — hence the repo root rather than `docs/`.

## Status

| | Step | State |
| --- | --- | --- |
| 1 | The page, with search/filter selection + preview + "send to project" | **done** |
| 2 | Apply a LABEL to a selection (the predictions path) | **done** |
| 3 | k-NN "more like this" endpoint + selector | next |
| 4 | `scripts/` labelling functions → Arrow → import | deferred — decision parked |
| 5 | Propagation with visible thresholds | needs 3 |
| 6 | Uncertainty selector | needs a trained model |

**Step 2 landed as PREDICTIONS, following Label Studio.** A send may carry `prediction: [Shape]`;
it rides the task document (one write, at send) and is deliberately NOT a draft — a draft is
submittable, and `save_draft` refuses a task that is not CLAIMED precisely so work drawn by nobody
cannot walk into review. `PredictionShape` is `Shape` minus `source`, which the server stamps
(`BULK_SOURCE`), so a sender cannot forge human provenance. The taxonomy closes over the WHOLE send
before the first actor is seeded, via the same `membership_violation` import and submit use.

The UI offers only classes the taxonomy allows as a whole-item `tag`; a class drawn only as a box is
withheld rather than sent to a guaranteed 409. The client cap now mirrors the server's
(`SEND_TASK_CAP = 1000`, divided by `consensus_n`) — it was 5 000, a number of the client's own
invention, and a 2 000-item selection passed every check and came back 422.

**Owner decisions still open:** #1 (uncertainty scores) is moot until a model exists; #2 is settled
as LLM-as-labeler → distil rather than Snorkel-style majority vote; #3 (where labelling functions
live) is parked until step 4; #4 (task granularity) settled as per-item with a cap.

---

## The problem, stated narrowly

Today one annotator labels one item at a time. The queue distributes work, the canvas draws it, the
review loop accepts it. That is correct and it does not scale: a corpus of 200 000 pages cannot be
labelled by drawing on each one, and the first 500 someone draws are usually the 500 *least*
informative — near-duplicates of each other, all easy, teaching a model nothing.

Bulk labeling is not "the same UI with checkboxes". It is a different question:

> Of everything here, **which few thousand items should a human touch**, and what can be labelled
> **without** a human touching them at all?

Those are two mechanisms — SELECTION and PROPAGATION — and conflating them is the main way this kind
of surface goes wrong.

## What already exists (and should not be rebuilt)

This is the strongest constraint on the design, so it comes first.

| Capability | Where it already lives |
| --- | --- |
| Search a corpus (FTS / vector / hybrid), filter by declared fields | `services/search` |
| Search SEVERAL corpora at once, fused by reciprocal rank | `?corpus=a&corpus=b` (landed) |
| A 2-D embedding projection of a corpus, with cluster ids | the atlas (`declared.atlas`, `AtlasSpace`) |
| Nearest-neighbour retrieval over embeddings | the declared vector bindings; `packages/ratch` retrieval |
| A closed label taxonomy, enforced | `LabelOntology` + `membership_violation` |
| Landing MACHINE-produced labels as reviewable work | the import path (#39): Arrow IPC → task DRAFT, `source=import`, never accepted |
| Distributing items to people, with review | the projects/tasks actor plane |
| Model predictions for one item | the assist producers |

**The consequential observation: bulk labeling is not a new write path.** "Produce candidate labels
for N items, land them as unreviewed predictions, let people adjudicate" is *exactly* what the import
endpoint already does for one task. Bulk labeling is that, applied to many tasks, with the candidates
computed instead of uploaded.

If this design does one thing, it should be: **make the bulk surface a producer of the existing
import payload**, not a second way to write annotations. Every provenance, ontology and review
guarantee then comes for free, and a bug in bulk labeling cannot put unreviewed rows into the
lakehouse — because nothing can.

## Shape

Three panes, one page, `/annotator/browse`:

```
┌ SELECT ─────────────────┬ PREVIEW ─────────────┬ ACT ──────────────┐
│ how to choose items     │ what you chose       │ what to do        │
│  · search / filter      │  grid of candidates  │  · label as …     │
│  · atlas region         │  with the current    │  · send to a      │
│  · nearest to an example│  label + confidence  │    project        │
│  · uncertainty          │  and WHY it was      │  · reject         │
│  · a labelling function │  chosen              │                   │
└─────────────────────────┴──────────────────────┴───────────────────┘
```

The middle pane is the load-bearing one. **Every candidate must say why it is here** — "0.51
confidence between `figure` and `caption`", "3rd nearest to the example you picked", "matched
`/fig\.\s*\d/`". A bulk surface that cannot explain its selection is a surface nobody can trust
enough to accept in bulk, which defeats the point.

## The five selectors

Each is a way of ordering the corpus. They compose — a selector narrows, the next re-ranks.

### 1. Query and filter — **free today**

Search + declared filters + the multi-corpus fan-out. Zero new machinery: it is the explorer's
existing search, called from this page.

### 2. Atlas region — **nearly free**

Lasso a region of the 2-D projection, take its rows. The projection and cluster ids are already
declared per atlas space. Useful because visual clusters are usually *semantic* clusters, so one
region is often one label.

### 3. Nearest to an example — **needs a small endpoint**

"More like this one." A k-NN query over the declared vector binding. The retrieval exists in
`packages/ratch`; what is missing is an endpoint taking a row key and returning its k nearest.

This is the highest value-per-unit-work selector and I would build it first.

### 4. Uncertainty — **the active-learning core**

Rank by how *unsure* a model is. Requires per-item model scores, which requires a model, which
requires labels — so this selector is **cold-start-empty by construction**. It becomes available
after a first round, and the UI must say so rather than showing an empty list.

Standard measures, cheapest first: least-confidence (`1 - max p`), margin (`p₁ - p₂`), entropy.
Margin is the usual best default for multi-class.

**DECIDE — where do the scores come from?** Three options:

- **(a) The assist producers, batched.** Reuses the producer contract and its ontology check. Slow
  for 200 000 items; fine for a sampled candidate pool.
- **(b) A stored `predictions` column** written by a medallion mover. Fast to query, and it makes
  scores a first-class artefact of the corpus. More moving parts.
- **(c) Nothing — skip uncertainty in v1.** Ship selectors 1–3 and 5, add this when a model exists.

I would ship **(c) first and design toward (b)**: uncertainty is worthless without a trained model,
and there is not one yet. Building it before that is building against an imagined consumer.

### 5. Labelling functions — **the weak-supervision core**

A labelling function is a small rule that votes on a label or abstains: a regex on the text, a
threshold on a declared column, a k-NN vote, another model's output. Several disagree; a label model
resolves them into a probabilistic label.

**DECIDE — how much of Snorkel's machinery is warranted?** The honest range:

- **(a) Majority vote.** Ten lines, no dependency, explicable to anyone. Weak when the functions have
  very different accuracies.
- **(b) A generative label model** (Snorkel-style) that learns per-function accuracies from their
  agreement pattern, without ground truth. Materially better when functions vary in quality; a real
  dependency and a real thing to explain.
- **(c) Skip weak supervision in v1.**

I would ship **(a)** and keep the seam so (b) can replace it. Majority vote over 3–5 functions on a
narrow taxonomy is usually within a few points of a learned model, and it is *auditable* — a
reviewer can see which rules fired. An unexplainable probabilistic label in a bulk-accept flow is
exactly the thing that produces a corpus nobody trusts.

**Where do labelling functions live?** They are user-authored code. Two options:

- **In `scripts/`, producing an Arrow file** that the existing import consumes. No execution surface
  in the service, no sandbox, no new attack surface. Matches the #39 ruling exactly.
- **In the product**, authored in a text box and executed server-side. Much better UX; requires
  sandboxed execution of user code, which is a serious undertaking.

**Strong recommendation: `scripts/` first.** The #39 argument transfers unchanged — conversion (and
now rule authoring) is a `scripts/` concern; the service accepts one canonical format. If in-product
authoring is later wanted, it can be added knowing exactly what payload it must produce.

## Propagation

Given a labelled seed, extend it: for each unlabelled item, find its k nearest labelled neighbours in
embedding space and adopt the majority label if agreement and distance clear thresholds.

Two knobs, both must be **visible and adjustable**, because they are the difference between a useful
corpus and a confidently wrong one:

- `k` and the **agreement floor** (e.g. ≥ 4 of 5 neighbours agree)
- a **distance ceiling** — beyond it, propagation refuses rather than guessing

Every propagated label lands as `source=propagated` with its neighbour set recorded, so a reviewer
can ask *why* and a later audit can find every row a given seed produced. `DraftOrigin` already has a
`propagated` member; this is what it was for.

## What comes out

**Not annotations.** Candidates flow into the existing pipeline:

```
selection → candidate labels → Arrow IPC (the annotations schema)
         → the import path → task DRAFTs, status=prediction, source=import|propagated
         → the ordinary queue / review / accept / publish loop
```

Consequences, all of them good:

- The ontology check runs — a labelling function cannot invent a label.
- Nothing reaches the lakehouse unreviewed, because nothing can.
- `#40`'s bulk-accept already exists for adjudicating them en masse.
- The `_dataset`/`_table` provenance from #28 rides along, so a multi-corpus selection stays
  attributable.

**DECIDE — one task per item, or one task per BATCH?** Per-item reuses everything and gives per-item
review, but 50 000 Dapr actors for one bulk action is a real cost. Per-batch is cheaper and needs a
new task shape. I lean **per-item, with a cap** (refuse a bulk action above ~5 000 items and say so),
because it changes no model and the cap is honest about the limit rather than hiding it.

## What this deliberately does NOT do

- **No auto-accept.** No path where a computed label becomes an accepted annotation without a human.
  If that is ever wanted it is a separate, explicit decision with its own audit story.
- **No training.** This produces labels; training on them is the `train` zone's job.
- **No new write path.** If bulk labeling needs to write annotations directly, this design is wrong.

## Build order

Each step is independently useful and independently abandonable.

See the Status table at the top. Atlas-region selection is folded into step 3 — both are ways of
ordering the corpus by embedding proximity, and building the k-NN endpoint first makes the lasso a
second caller rather than a second mechanism.
