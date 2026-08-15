# open_tasks — the pinned engineering task list

**What this is.** The OPEN backend/platform tasks, in one place, because the live list (`/tasks`) dies
with the session. **Not** `TODO.md` — that is the product/frontend backlog (26 items: routes, sidebar,
Explorer, annotate, studio).

**This is an INDEX, not a copy.** Every item points at the document that owns it. A second full
statement of a task drifts from the first, and then nobody knows which one is true. Read the source
before starting; update the source, not this file, when the work moves.

**Closed items are not listed. Git history is the record of those** — and that rule is load-bearing,
not tidiness. This file is itself an `open_*.md`: ephemeral by design, deleted when the queue empties.
Writing history into it means writing history into something scheduled for deletion. Nothing here may
be the only copy of anything.

**Nothing may cite this file as an ADDRESS.** Durable code — Python, chart templates, YAML — cites no
`open_*.md` at all. A sibling plan may restate a claim from another inline; it may not link to one.
`open_dapr.md` accumulated 74 such citations across 45 files before it was deleted 2026-08-15, and
every one had to be rewritten first.

**Scope.** The wider estate queue — frontend (#110, #111, #116, #130, #147), owner rulings (#98, #134,
#143, #146), and the lakehouse/catalog items (#43, #48, #56, #67, #84, #85, #91, #142) — lives in the
session task list and in `open_lakehouse_diff2.md` §5. `diff2` §5 row 2 (F2, time-boxed grants) is
**half done**: the wrapper half landed 2026-08-14 (`b58eff4f` — `condition_context()` + `context` on
all four read wrappers); the catalog call-site half (`_require` passes no context) is still open.

---

## The helm window. Yours, because it replaces every running image.

Chart changes are committed and render correctly but are NOT in the release. `make k3s-up` owns it —
a hand `helm upgrade` with different values replaces every deployed image with the chart default.

- `lance-statestore` now scoped to the mover app-ids. **daprd cannot hot-reload an actor state
  store**, so a mover pod that started before this keeps the OLD scope list and fails to dispatch on
  every delivery. The medallion deployments need a restart. The workflow is new, so no in-flight
  instances exist and **no drain is required**.
- `medallion.compute` / `medallion.ray` now default true, with `rayAddress` derived when empty. This
  supersedes the env I set by hand on `rask-bronze-to-silver` while driving S1 — the upgrade replaces
  improvised state with declared state.
- `MAINTENANCE_ORPHAN_SCAN_ENABLED` — the running maintenance pod carries no such variable at all.
- The observability retention floor, and `dapr.io/config` gated on `dapr.enabled`.
- The otel-collector app-log filter (applied by hand 2026-08-15; the upgrade makes it durable).

---

## Medallion — owned by `open_medallion_workflow.md`, not restated here

Two items were listed here as open and were WRONG, because they were second statements of things that
doc already owns and had already settled. This is what the index rule is for; read the owner first.

- The promotion review band is **DECIDED** (±25%, plus first-promotion-of-a-dataset) — §9 item 1.
- The workflow management surface is **DESIGNED**, not an open question:
  `POST /api/medallion/promotions/{id}/decision`, FGA-gated on `can_promote`, mounted inside
  `RASK_API_PREFIX`. Unbuilt because it is S3.

That doc also owns the constraint that an activity payload must fit **4 MiB** — the workflow worker's
own gRPC channel, not daprd's `--max-body-size`. Deriving that number independently instead of reading
it there shipped a flows bound at 4x the real ceiling (`f95be037` corrects `0bbcc035`).
