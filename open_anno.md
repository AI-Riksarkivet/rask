# open_anno — the annotation plane's REMAINING work

Working plan, not settled architecture. It does not belong in `docs/`, which asserts "settled" by
location regardless of contents. **Delete this file when the last section below lands.**

## What this file is NOT

Everything delivered has been REMOVED from here, because git history is the record and a plan file
that also describes finished work cannot be read as a to-do list.

Landed and gone from this file: **#37** (send refuses an unresolvable dataset; item removal; the read
path reporting the server's real reason), **#41** (relations end to end — ontology, storage,
submit-time validation, the two-click editor, the drawn canvas edge — and text spans), **#40a–40d**
(queue filter, bulk assign, per-annotator metrics, membership), **#27** (the canvas rail banded
navigate · draw · assist), **#43** (the three JSON columns became `pa.json_()`; a filter now reaches
inside them), **#39** (annotation import — ONE canonical format, Arrow IPC into the task draft, plus
the `scripts/` COCO converter), and **#31** (the explorer sidebar derives from the descriptor).

Two things remain. ONE is a decision, not a build; the other is the owner's to design.

---

## #45 — Table-level selection — **LANDED**

`search.row_table` was ONE fixed table, so a corpus exposed one searchable table however many it
held. Now `Declared.searches` is a named list mirroring `atlas`, `?table=` selects one, and the
picker has a second level. Legacy `search: {...}` still loads and is still served, so no descriptor
on disk changed.

The half that would have been silent: the picker relabelled and re-gated the sidebar while
`/api/search` still went to the default table — the client appended `?dataset=` everywhere and
`table` nowhere. `table` now travels as a module-level selection beside the active view, the same
route `dataset` already takes.

The Atlas nav gate is table-sensitive (spaces are bound to a table); Tree and Graph are not
(`capabilities` is per corpus). That asymmetry is a property of the descriptor, recorded in `nav.ts`.

---

## #28 — Multi-dataset search — **BLOCKED on a ranking decision**

The rest of #28 landed: the dataset **picker** (`dataset-picker.svelte` + the pure
`dataset-choice.ts`), the sidebar **Guide** row is gone, and the lakehouse `/catalog/projects` route
turned out to have been deleted already by the 2026-08-03 IA ruling (`140315e`) — verified, not
assumed.

What is left is searching ACROSS corpora, and it is blocked on a question rather than on effort.

The *transport* is ready: `GET /api/search` already takes an optional `dataset`, so fanning out N
calls is straightforward. What is not ready is the RENDER path. Every hit renderer resolves its
display fields through the module-level `activeView()` singleton — **61 call sites across 27 files**
(`hit-card`, `hit-table`, `doc-tile`, `player-pane`, `transcript-window`, `chunk-timeline`, the atlas,
the workflow nodes, `utils.hitKey`, …). Two corpora have different column names, so a merged result
list cannot be rendered until a per-hit `DatasetView` is threaded through all of them. That is a
substantial refactor of the zone's core, not an afternoon.

And before doing it, one question needs an answer that is not the implementer's to pick: **scores are
not comparable across corpora.** BM25 is normalised per index and vector distances depend on the
space, so "merge by score and interleave" produces a ranking that looks authoritative and means
nothing. The alternatives are materially different products:

- **Grouped per corpus** — N result lists side by side, each internally ranked. Honest, no
  cross-corpus scoring claim, and it needs no per-hit view because each list has ONE view.
- **Interleaved with per-corpus quotas** — one list, k hits from each, ordered within each corpus.
- **True fused ranking** — requires a comparable score (reciprocal-rank fusion is the usual answer)
  and is the only option that needs the full per-hit-view refactor.

**DECIDED by the owner: true fused ranking (RRF).** The most defensible single list, and the most
work — it needs the full per-hit `DatasetView` refactor, which is the same refactor #43's second half
needs (media kind and pane capabilities are resolved per DATASET, not per ROW). Do them together.

---

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
