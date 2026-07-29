# open_anno — the annotation task-management frontend

Asked for on 2026-07-29. **Not started.** The backend is built and on `main`; there is **no UI for it
at all**, which is why the work looks missing when you drive the app.

## The state of play, measured not assumed

**Backend — exists, 9 files on `main`:**

```
services/annotator/src/annotator/projects/
  models.py         four documents, two state machines, one publish record
  machines.py       the two state machines, AS DATA (not code branches)
  actor.py          the annotation-TASK actor — the first registered actor in the estate
  project_actor.py  the annotation-PROJECT actor — "the one that makes publish decidable"
  saga.py           publish → lakehouse, crash-safe without a workflow engine
  publish.py        which annotations land, in what shape, whose names travel with them
tests/unit/test_annotation_projects_machine.py
tests/unit/test_annotation_projects_create_pin.py
```

Relations and transitions already modelled: `open` · `claim` · `assign` · `accept` ·
`fix_and_accept` · `freeze` · `archive` · `publish` · `lease_expired`, gated by `can_claim` ·
`can_annotate` · `can_review` · `can_manage` · `can_publish` · `can_send_items`.

**Frontend — does not exist.** The `annotator` zone ships exactly ONE page (`/annotator`, the
PixiJS/WebGPU canvas) plus BFF proxy routes. **Zero** files under `frontend/microfrontends/annotator/src`
reference the projects plane. There is no project list, no task queue, no claim button, no review
screen, no publish action.

So: a CVAT / Label-Studio-shaped task system with a complete backend and no way to reach it.

## What to build

### A1 · Project list and detail

The entry point that does not exist. List annotation projects, create one, open one. Project state
comes from `project_actor`; do not re-derive it in the client — `machines.py` holds the transitions
**as data**, so the UI should render the legal transitions it is given rather than hardcoding a second
copy that drifts.

### A2 · The task queue — claim / annotate / submit

The annotator's working loop. `actor.py` is the task actor and owns the lease: `claim` takes one,
`lease_expired` returns it. That means the UI has to show lease state honestly — a claimed task whose
lease died is not the annotator's any more, and showing it as theirs invites lost work.

Route into the existing canvas: the annotator zone already renders Arrow-backed rows on a WebGPU
canvas. A task should open THAT, not a second viewer.

### A3 · Review — accept / fix-and-accept / reject

`can_review` gates it; `fix_and_accept` is a distinct transition from `accept` and the UI must not
collapse them — the whole point of the separate edge is that a reviewer changed something, and
publish provenance depends on knowing which happened.

### A4 · Publish

`saga.py` is crash-safe without a workflow engine, which means it is **resumable and observable** —
surface where a publish is, not just a spinner. `publish.py` decides which annotations land and whose
names travel with them, so the confirm step must state both before it runs.

## Constraints

- **`annotator` is the least strictly typed zone in the estate** — it extends
  `@rask/config/tsconfig.base.json`, which sets neither `noUncheckedIndexedAccess` nor
  `exactOptionalPropertyTypes` (`rask-frontend` § *TypeScript strictness is split*). That is called a
  defect there, not a design. New code should not lean on the looseness.
- **Data dialect:** `annotator` is a same-origin-BFF zone (`+server.ts` proxy + `createBffClient`),
  NOT remote `query()`. Match the zone (`rask-frontend` § *Fetching data*).
- **Authorization is real here.** `can_claim` / `can_review` / `can_manage` are FGA relations, not UI
  state. Gate server-side and let the UI reflect the answer; a disabled button is not a permission.
  See `open_fga.md` — the estate currently has no bootstrap path, so a fresh deploy has zero tuples
  and every gate answers "no" for everyone.
- **Components come from `@rask/ui`.** A styled `div` in a zone is a bug report against that package
  (`rask-styling`). A task queue is a data-table; one already exists.
- If this grows a docked workspace, it inherits `open_dockview.md`'s four invariants.
