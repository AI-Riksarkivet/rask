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

ONE thing remains, and it is the owner's to design.

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

## #28 + #43 — Multi-corpus search and per-hit rendering — **LANDED**

Both, together, because they were one refactor: fusing results from several corpora is pointless if
the renderer cannot tell which corpus a row came from.

- Every hit carries `_dataset` / `_table`, stamped in the one funnel all search paths return through.
- `fuse.py` — reciprocal-rank fusion, the ranking chosen over grouped and quota-interleaved. Scores
  are discarded because BM25 is normalised per index and vector distances depend on the space.
- `?corpus=a&corpus=b` fans out; a refusing corpus is skipped with its reason logged rather than
  killing the request.
- `viewForHit(hit)` resolves a hit through ITS OWN corpus's view. That also closed #43's second half:
  media kind and pane capabilities were resolved per DATASET, so a mixed corpus could not render
  correctly no matter what the pane gates said.
- The picker's "+ also search" toggle, and — the load-bearing part — the store REGISTERS each
  fanned-out descriptor. Requesting without registering leaves every foreign row rendering as if it
  belonged to the active corpus, and it renders, so nothing reports it.

**A design error the tests caught, kept because it is the sort that repeats:** the first RRF identity
keyed on `(dataset, table, keys)`, so nothing could ever compound — which silently degenerates RRF
into the rank-interleaving that was explicitly not chosen. The rule is asymmetric: `_dataset` yes,
`_table` no. Across corpora a shared id is coincidence; across tables of one corpus it is the same
document at two granularities, and that agreement is what fusion exists to reward.

Live: `?corpus=voices` -> picker "demo +1"; the voices hit renders `<audio>` while `demo` (an image
corpus) stays active.

---

## #42 — `/annotator/browse` as the bulk-labeling surface

**Owner is designing this.** Intended direction, from the owner: select data by active learning,
AI-assisted selection, and bulk labelling with weak supervision + embeddings. Do not design it
speculatively — it stays out of scope until that design exists.
