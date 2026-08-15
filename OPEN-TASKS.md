# OPEN TASKS — pinned snapshot (2026-08-14)

A snapshot of the session task list, pinned here so it survives a session and is readable by the
other live sessions. **Not a second source of truth**: the long-form durable register is
`OPEN-WORK.md`, the table-maintenance residue is `open_table_maintenance.md`, and the catalog
comparison work is `open_lakehouse_diff2.md`. This file is the index over them.

**27 open.** Regenerate by listing the task tool; each `#N` below is its task id.

---

## ⛔ Blocks everything — fix first

| # | What | Next action |
| --- | --- | --- |
| **148** | `medallion/workflow.py` imports `dapr.ext.workflow`; `services/medallion/pyproject.toml` declares only `dapr` + `dapr-ext-fastapi`. The workspace venv hides it via a sibling; the image's own closure cannot, so the dockerfile import gate fails and **every `rest-catalog` build dies** — that image serves catalog, lineage, medallion, maintenance, viewer, search, annotator. | One line: add `"dapr-ext-workflow>=1.18",` (precedents: `services/flows/pyproject.toml:19`, `services/ingest/pyproject.toml:22`), then refresh the root `uv.lock`. Owner: the medallion S1 session, or approve another session to do it. |

## 🧑‍⚖️ Waiting on an owner ruling — do not start without one

| # | What |
| --- | --- |
| 98 | 74 craft judgement-calls on the `rask-*` skills — restructures the owner must weigh |
| 134 | The cross-zone navigation flash is ACCEPTED behaviour per `tokens.css` — reopen only to overturn |
| 143 | RULING already given ("every gated action shows disabled with its denial reason, never hidden") — the estate-wide pass is unstarted; the #147 audit found the concrete sites |
| 146 | RULING given ("no secret in env anywhere"). Survey COMPLETE, scope exact: only `explorer.yaml` (AWS_SECRET_ACCESS_KEY env) and `secrets.yaml` remain; needs `packages/storage` to resolve the secret half from the Dapr store like `service-kit`'s media config already does |
| 77 | Roles + identities as managed surfaces — CONDITIONAL, only if the owner wants it |
| 82 | TRIPWIRE — **deliberately do nothing**; interactive-frequency listings would trigger a query-store design round |

## 🧹 Table maintenance — handoff-ready (`open_table_maintenance.md` has the detail + STANDS/MOVED tags)

| # | What | Status as re-verified 2026-08-14 |
| --- | --- | --- |
| 128 | Six undisclosed scope cuts | **(a) orphan scan is production-dark** — `orphan_scan_enabled` defaults False, chart ships no lever, and `report_is_clean` does not block on the skip, so a purge can certify an estate whose file layer was never inspected. **(d) `purge.delete_location` deletes a dataset dir with no `base_paths` check** → destroys a live shallow clone. (b)(c)(f) MOVED — verify before implementing. (e) HANDS OFF |
| 114 | Compacting a shallow clone's SOURCE breaks the clone (reproduced: 8 files → 1, clone then fails to open cold; same-process reads lie via cache) | Same hazard class as 128(d) — **one pre-pass fixes both; doing either alone re-opens the other** |
| 60 | Reindex-from-scratch: what `optimize_indices` cannot do | Detect and REPORT only; rebuild stays an operator action |
| 61 | Fragment sizing + conflict policy (`target_rows_per_fragment`) | A decision, not a bug — conflict detection is per-fragment and the annotator writes concurrently; decide per tier, land as policy (#51) |
| 62 | Maintenance observability (LANCE tracing → Greptime) | **IN FLIGHT by another session** — `lance_trace.py` is untracked with a modified `lineage_emit.py` beside it. Coordinate, do not delete |

## 🗄️ Lakehouse / catalog

| # | What |
| --- | --- |
| 43 | Multi-tenancy is one rustfs + one credential — separate instances are not expressible |
| 48 | Partial-failure honesty is unmeetable in the warehouse delete's response shape |
| 56 | Exercise Lance multi-base layout (one table spanning buckets) |
| 67 | Migrate pre-registry ghost projects (FGA tuples with no registry record) |
| 84 | Run the credential attack in CI — needs web_identity vending + a second tenant admin (diff2 §5 row 3 / F5) |
| 85 | Collapse the four control-root JSON stores into one shared record primitive (diff2 row 4 residual) |
| 91 | Column-level classification tags — the sekretess/GDPR lever the estate cannot express |
| 142 | The corpus seed bypasses the catalog — take it through the real doors, as `seed_estate.py` did |

**diff2 §5 row 2 (F2) is HALF DONE**: the wrapper half landed 2026-08-14 (`b58eff4f`) — `condition_context()` + `context` on `check`/`batch_check`/`list_objects`/`list_users`. The **catalog call-site half** (`_require` passes no context) is still open, deferred because `services/catalog` was being edited by another session.

## 🖥️ Frontend

| # | What |
| --- | --- |
| 110 | Project in the URL — host-carried scope wired end-to-end |
| 111 | Collapsed sidebar: the icon-mode header (project switcher) overflows the rail |
| 116 | Estate-wide svelte-check "SnippetReturn unique symbol" identity split |
| 130 | Project rung hard-codes `protected="false"` at create; no frontend caller for the protection reads |
| 147 | Lakehouse progress/toast/notification audit — **audit COMPLETE** (50 agents), findings confirmed incl. two high-severity loading-state bugs; implementation not started |

## 🏗️ Platform / infra

| # | What |
| --- | --- |
| 135 | The chart has ONE `image.tag` but the cluster runs four different references — `helm upgrade` is a destructive act |
| 115 | Cleanup sweep: the audit's remaining reuse / dead-code / altitude findings |

---

## Two traps for anyone picking these up

- **The worktree at `/home/blackwell/Desktop/rask` is SHARED between live sessions.** Never
  `git add -A`, never stash; commit named paths only and push via a throwaway worktree if a merge is
  blocked. `origin/main` moves under you.
- **A bare `kubectl` reaches a stale kind cluster.** The live release is k3s —
  `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` (the Makefile already sets it).
