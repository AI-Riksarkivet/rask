# open_fga — an in-estate OpenFGA playground

Asked for on 2026-07-29. **F1–F3 shipped; F4 half-shipped.** Kept rather than deleted because the
remaining half is real work and the findings below cost a day to learn.

| goal | state |
| --- | --- |
| F1 read the live graph | **done** — the whole tuple store on one canvas, layered and crossing-minimised |
| F2 spotlight a subject/object | **done** — what-can (ListObjects) / who-can (ListUsers) / why (Check+Expand), derivation lit hop by hop |
| F3 edit tuples | **done** — grant + revoke, blast radius measured before the write |
| F4 assertions | **half** — `fga.check(contextual_tuples=…)` exists; no `/v1/access/simulate`, no `.fga.yaml` export |

## What the build actually taught us

- **A subject is the OIDC `sub`, not a username.** Under Dex that is a base64 protobuf
  (`CiQwOGE4Njg0Yi1kYjg4…`). Typing `user:alice` yields a CORRECT denial that reads exactly like a
  bug, and it is the single most confusing thing about the store. The UI seeds the field from
  `/v1/me` and offers a "me" chip; anything new touching subjects must do the same.
- **Expand answers QUALIFIED usersets.** `computed.userset` is `"table:x#reader"`, not `"reader"`.
  Passing it back as a relation is a malformed request → OpenFGA 500 → a fail-closed 503, so every
  relation resolving through a rung reported "derivation unavailable".
- **A fresh store holds three tuples.** `bootstrap-admin.yaml` grants a first owner; nothing seeds an
  estate. `scripts/fga_seed_demo.py` loads `model.fga.yaml`'s 27 fixtures so the graph has something
  to show, remapping alice/bob to their real Dex subs.
- **"Who granted this" is NOT answerable today**, and the gap is coverage, not plumbing. Audit rows
  reach GreptimeDB and the lakehouse already has a SQL BFF over them, but only **2 of 6** tuple-write
  sites emit a tuple-level row — `grant_on_create` (every table/namespace create) does not. A
  provenance UI built before that is fixed would render blanks for most tuples. The fix is to move
  `audit()` inside `fga.write_tuples`/`delete_tuples` with a **required** `actor`, so a new write site
  cannot compile without naming a principal. Note also the 14d GreptimeDB TTL.
- **OpenFGA's changelog carries no actor.** A `TupleChange` is `{tuple_key, operation, timestamp}`.
  `fga.read_changes()` exposes it and deliberately returns five fields — an `actor` key, even null,
  would invite a UI to render a timestamp as attribution.
- **`read_changes` pagination has a trap**: OpenFGA returns the same non-empty continuation token
  forever at end-of-stream, so `while token:` never terminates. Stop on an empty page.

## Still to do

1. `POST /v1/access/simulate` — assert-before-grant in the UI. Cap contextual tuples at 10; return
   `baseline` alongside `allowed`, because the DELTA is the answer, not the verdict.
2. F4's export half — emit a checked assertion as `.fga.yaml` so a finding becomes a CI gate.
3. The audit `actor` fix above, before any provenance UI.
4. `governance/+layout.server.ts` collapses 401 and 403 into one message that asserts you lack a
   privilege — when you may simply have no session. It cost real debugging time.
5. Decide `observability.retention` (14d today) if provenance is meant to be compliance-grade.

The goal in the owner's words: a view *like the OpenFGA playground* — visualise the tuple graph so a
human can see how authorization actually resolves today, spotlight one user or one object, edit
tuples, and write assertions.

Kept in its own file rather than in `OPEN-WORK.md` because this is one feature area with its own
lifetime, the same reason `open_dockview.md` is separate.

## Why this is worth building rather than using the hosted playground

The hosted playground models a **hypothetical** store you paste into it. The question that actually
costs time here is *"why can alice not see this table **right now**, in this cluster"* — which needs
the LIVE store, the live tuples, and the live model. That is exactly what today lacks, and it is not
hypothetical: on 2026-07-29 the catalog answered alice `{"tables":[]}` with HTTP **200** while
`bronze$pages` was registered and present on disk. Nothing in the UI could say whether that was an
empty catalog, a broken query, or a denied read — and the difference is the whole answer.

A governed estate whose authorization is unreadable is one where every access question becomes a
`kubectl exec` and a guess.

## What exists to build on

- **The model** — `packages/service-kit/src/service_kit/governed/auth/model.fga`, 7 types
  (`user`, `team`, `role`, `project`, `warehouse`, plus the table/annotation surfaces) with concentric
  rungs: `reader ⊆ writer ⊆ owner`, `member ⊆ admin`, and `can_*` permissions derived from them
  (`can_create_table: writer`, `can_get_metadata: reader`, …). It exists in **three copies** —
  `model.fga` authored, `model.fga.yaml` tested, `model.json` loaded by the app — and CI diffs the
  transform of the first against the third, so drift already fails a gate (`ms-authz`).
- **One UI surface** — `lakehouse/src/routes/governance/access/+page.svelte`, and `@rask/ui`'s
  `GrantsPanel`, which takes a `client` prop of async functions (the library owns no transport).
- **`.claude/skills/openfga`** — the modelling conventions this must not contradict.
- **The store itself** — OpenFGA with a CloudNativePG database, `openfga.enabled=true` by default.

## F1 · Read the live graph

A tuple list is not a graph. The value is in the **derivation**: `alice → member of team X → admin of
project Y → owner of warehouse Z → reader of table T`. That chain is what answers "why", and it is
what a flat tuple table hides.

- Source the edges from the OpenFGA API — `Read` for tuples, `Expand` for a relation's userset tree.
  `Expand` is the one that returns the derivation rather than the assertion, so it is the backbone.
- Render with **Svelte Flow** (`@xyflow/svelte`), which the estate already uses in `lakehouse` and
  `media` — see `.claude/skills/svelte-flow`. Not a new graph library.
- Layout: the model is a DAG of types with concentric relations, so a layered/dagre layout reads far
  better than force. `svelte-flow`'s skill covers the dagre helper already in use.

## F2 · Spotlight a subject or an object

Two directions, and both are needed because they answer different questions:

- **From a user** — "what can alice reach, and by which path?" This is `ListObjects` per type, plus an
  `Expand` per hit to recover the chain.
- **From an object** — "who can read `bronze$pages`, and why?" This is `ListUsers` / `Expand` on the
  object's relations.

Highlight the resolving path and dim the rest. A permission that resolves through three hops looks
identical to a direct grant in a tuple table, and they have very different blast radii when revoked.

## F3 · Edit tuples

Write and delete tuples from the view, against the live store.

- **This is a privileged, destructive surface.** It belongs behind the same estate-admin door the rest
  of `/lakehouse/governance/*` already uses (`governance/+layout.server.ts`, fail-closed), and every
  write should be attributable — the audit view exists and should receive these.
- Deleting a tuple can revoke access for many principals at once when it sits high in the DAG.
  Show the **blast radius before the write**: run `ListUsers` for the affected relation and say "this
  removes access for N principals", not "are you sure?".
- Never edit the MODEL here. The model is source-controlled in three files with a CI drift gate; a UI
  that mutates it would make the gate a liar. Tuples are data; the model is code.

## F4 · Assertions

The `.fga.yaml` format already supports `check`, `list_objects` and `list_users` assertions, and
`ms-authz` runs them in CI (`fga model test --tests model.fga.yaml`).

- Let a user compose an assertion **from the graph they are looking at** — pick a subject, a relation,
  an object, expect allowed/denied — and run it against the live store for immediate feedback.
- The payoff is the export: emit the assertion as `.fga.yaml` so it can be committed into
  `model.fga.yaml` and become a CI gate. That closes the loop from "I found a surprising grant" to
  "this can never regress" without hand-writing YAML.
- Distinguish clearly in the UI between an assertion **run live** (a fact about this cluster now) and
  one **committed** (a fact CI enforces). They are not the same claim.

## Constraints

- The model is the contract: 7 types with concentric rungs. A playground that lets someone reason
  their way to a different model is fine; one that silently drifts from `model.fga` is not.
- `@rask/ui` stays transport-agnostic — panels take a `client` prop of async functions, they do not
  import `@rask/api`. `GrantsPanel` is the precedent.
- Reads must be governed too. The playground shows who-can-do-what, which is itself sensitive; it
  cannot become a way for a non-admin to enumerate the estate's principals and objects.
