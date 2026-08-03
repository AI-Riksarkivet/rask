# open_transport — LANDED 2026-08-03

One transport per payload KIND. Typed app **values** ride SvelteKit remote functions; **bytes**
(Arrow, blobs, multipart, the OIDC redirect flow) stay on `+server.ts`. Both halves are idiomatic
SvelteKit — devalue 5.8.1 *can* carry binary (base64, `stringify.js:308`); we don't, because it costs
+33% on the wire, triple-buffers, and loses HTTP semantics.

This file is the record of what the 70-route verdict table resolved to, and of what it did NOT.

> Kept rather than deleted, against the plan-doc convention: **57 comments across 50 source files
> cite this path** as the rationale record for a route that is now gone. Folding it into
> `docs/architecture/frontend-conventions.md` is the right end state and is a 50-file comment sweep —
> deferred deliberately, not forgotten.

## Outcome against the verdicts

| verdict | count | landed |
| --- | --- | --- |
| `remote-fn` | 56 | deleted; surfaces on `.remote.ts` |
| `keep-bytes` | 10 | byte-identical, untouched |
| `promote-arrow` | 2 | Arrow IPC, consumers read `tableFromIPC` |
| `collapse` | 1 | media's dead `jobs/[...path]`, deleted |
| `keep-flow` | 1 | `capi/v1/me` |

| zone | `+server.ts` before → after | `.remote.ts` modules | `requestJSON` |
| --- | --- | --- | --- |
| `lakehouse` | 41 → **8** | 15 | 0 |
| `media` | 13 → **6** | 5 | 0 |
| `annotator` | 9 → **3** | 6 | 0 |

Estate-wide: `command()` 59 across 17 modules, `form()` 0, `query.batch()` 0. Every remote function
returns an `ApiResult<T>` union, parses its wire with valibot, and answers `{ok:false,status:0}` when
the upstream is unreachable — a rejected fetch must never cross the remote boundary.

**The two promote-arrow routes keep their `+server.ts` because their REQUEST shape pins them there**
(`api/search` carries a File; `api/atlas/chunks` is a read spelled as a POST). They answer Arrow built
by `media/src/lib/server/rows-arrow.ts`, which derives the schema per response because a corpus row is
a loose object, and names the JSON-carried columns in the schema metadata.

## What did NOT land, and why

- **The two lakehouse catch-alls (`capi/[...path]`, `api/[...path]`).** 17 reads reach them from the
  workbench zone's custom-element bundles, and an element cannot import a `.remote.ts`. A blocker,
  not an oversight — `frontend/microfrontends/lakehouse/src/lib/store.ts:16` is the entry point.
- **`/api/audit`** — the same blocker in miniature: a thin shim over `lib/server/audit-core.ts`, which
  the zone's own remote function also calls, so gate, SQL, flattening and filters cannot drift between
  the two doors. Delete it the day elements gain a sanctioned way to reach a remote endpoint.
- **`capi/v1/me` in three zones** — keep-flow by verdict.
- **The fold of this file into the canon** (above).

## Two product defects the suite surfaced, neither transport-related

Both were invisible until the zone suite was run against the finished state:

1. `routes/lineage/+page.svelte` mounted `LineageGraph` without `base`/`navigate`, so every node click
   called an undefined callback — the canvas was a dead end. Those are props on purpose (the graph is
   also compiled as a custom element, where `$app/*` does not resolve); the element passed them and
   the page never did.
2. `packages/flow/src/layout.ts` sized the isolated-node grid at `layers.length`, which is **0** for an
   estate with no edges at all — collapsing it to one column and rebuilding the tower the partition
   exists to prevent. A fresh lakehouse, before any OpenLineage event, is exactly that estate.

## The lesson worth keeping

`0acc8d4` shipped the media zone SERVING Arrow while the decoder that reads it sat uncommitted in the
working tree — main served Arrow bytes to a JSON parser for two commits. "Commit own paths only" is
correct in a shared worktree, but a path list is a manual list: **a change spanning a zone AND a
package needs both halves named.** Verify with `git show HEAD:<file>`, never with a green local suite —
the local suite tests the working TREE, which is exactly what a partial commit does not ship.
