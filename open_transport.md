# open-transport — every payload on its correct transport

Audit complete, 2026-08-03 (3-agent fan-out, all 70 `+server.ts` routes in
lakehouse/media/annotator read with their consumers; per-route evidence in the session's
workflow transcript `wf_904f2526-591`). Implementation has NOT started — §Goal is the
/goal-ready condition for driving it in this session.

**The rule (also in the rask-frontend skill, §Fetching data):** one transport per payload
kind — typed app **values** → remote functions (valibot, `ApiResult` unions,
`query.live` where a change signal exists, single-flight mutations; reference:
`lakehouse/src/lib/admin/remote/access.remote.ts`); **tabular/bulk/binary** → Arrow
IPC / raw bytes on `+server.ts`, streamed. Both halves are idiomatic SvelteKit. devalue
5.8.1 *can* carry binary (base64, `stringify.js:308`) — we don't, because +33% wire,
triple buffering, no streaming, no HTTP semantics.

## Verdicts: 70 routes

| verdict | count | meaning |
|---|---|---|
| `remote-fn` | 56 | JSON value surface → becomes a remote function; the route is DELETED |
| `keep-bytes` | 10 | already Arrow/binary/multipart/href-addressed — untouched |
| `promote-arrow` | 2 | tabular JSON → becomes Arrow IPC bytes |
| `collapse` | 1 | dead/subsumable route — deleted without replacement |
| `keep-flow` | 1 | identity endpoint kept for the shell's browser-side read |

**Honest performance verdict:** this round is ~85% consistency and deletion (56 routes,
most of them the same copy-pasted ~30-line bearer-forward template, replaced by
per-domain `.remote.ts` modules), **two** genuine Arrow wins, one live bug fixed for
free, and `query.live` coverage wherever a cursor exists. It is not a speed round; the
speed was mostly already right — the genuinely tabular lakehouse flows (table
preview/query, insert) **already speak Arrow**, which this audit verified and which
corrects an earlier guess that `table/query` needed promotion. Do not oversell it.

## The two Arrow promotions (the only perf-motivated changes)

- `media/api/atlas/chunks` — up to **1000 full rows as `list[dict]` JSON per lasso
  selection**; the clearest offender in the estate. Becomes Arrow IPC.
- `media/api/search` — the same `Row[]` payload kind at n≤100; promoted for transport
  uniformity (its multipart-POST input pins it to `+server.ts` anyway — the *response*
  becomes Arrow).

## Found defects & port caveats (from the classifiers' evidence)

1. **LIVE BUG, fixed free by the migration:** `attachStore`
   (`lakehouse/src/lib/storage/storage.ts:62`, used by `AttachStore.svelte:37`) POSTs
   `/capi/v1/stores`, but the catch-all proxy is **GET-only** — guaranteed 405; the
   attach-store UI is dead today. Becomes a `command()`.
2. **Cross-zone consumers on the lakehouse catch-alls:** the workbench custom elements
   bind `createBffClient('/lakehouse')` (`src/lib/elements/store.ts:14`) and ride the
   catch-alls with the session cookie. Remote functions are served under the same zone
   base, so element scripts can migrate too — **verify before collapsing the
   catch-alls**; if elements can't consume remote endpoints, the catch-alls stay until
   they can (listed as deferred, not silently kept).
3. **Lineage read fallback:** `makeLineageProxy` carries a READ-only service-token
   fallback (`packages/api/src/bff.ts:190-192`). The lineage `.remote.ts` port must
   preserve exactly that semantic (reads fall back, writes never).
4. **Media's `jobs/[...path]`** is dead/subsumable (`collapse`) — `jobs/apply` is the
   only live entry.
5. **Annotator's Arrow labeling transport is exactly one route**
   (`api/annotations/[...path]`: Arrow IPC + `X-Annotations-Version` + 409 contract) —
   untouchable. `draft-sync` deliberately reads Arrow but writes JSON drafts — that
   asymmetry is a contract, not an inconsistency.

## Keep-bytes list (untouched, for the record)

lakehouse: `capi/v1/table/[id]/query` (Arrow out), `…/insert` (Arrow in),
`…/[...rest]` (blob `<img>` bytes + history), `api/media/[...rest]` (downloads/page
images — `content-disposition` is the contract, hrefs must stay GET routes).
media: `api/atlas/points` (Arrow already), `api/voice/similar` (multipart in),
`api/[...path]`, `diagram` (SVG bytes). annotator: `api/annotations/[...path]` (THE
Arrow transport), `api/[...path]`.

## Execution order (area by area, each landing green before the next)

1. lakehouse `lib/data` + `lib/storage` (catalog/namespace/table/warehouse/stores
   values — the big template family; fixes the 405 bug)
2. lakehouse `lib/lineage` + `lib/models` + admin remainder (audit, jetstream,
   experiments, dlq-replay, medallion actions; preserves the service-token read
   fallback)
3. media (cypher, tags, jobs-apply, projects, user-state, me + the two Arrow
   promotions)
4. annotator (config, projects, tasks, assist, jobs)
5. catch-all collapse — only after the cross-zone element verification (caveat 2)

Per area: `.remote.ts` module on the access.remote.ts template → consumers repointed →
routes deleted → hermetic e2e reworked on the mock-upstream pattern → zone gates green.

## Goal (paste when ready)

> **/goal** Every route in open_transport.md's verdict table rides its verdict:
> the 56 remote-fn routes are DELETED with their surfaces on `.remote.ts` functions
> (valibot args, ApiResult unions, `query.live` where a cursor exists); the 2
> promote-arrow responses serve Arrow IPC and their consumers read `tableFromIPC`; the
> 10 keep-bytes + 1 keep-flow routes are byte-identical untouched; media's dead
> `jobs/[...path]` is gone; the attach-store 405 is fixed and covered by a test; the
> catch-alls are collapsed OR their cross-zone element blocker is documented in the
> final report (no silent keeps). Prove each clause in-transcript: per-zone e2e run
> outputs green (hermetic suites reworked on the mock-upstream pattern), `bunx turbo
> --cwd=frontend run check lint` 0/0, zone-contract suite green, autofixer issues=[] on
> every touched .svelte, and `git status` showing only owned paths committed. The
> rask-frontend skill's route counts are updated in the same commits. Constraint:
> concurrent sessions are active — commit own paths only; any deferred route is named
> in the final report with its reason. Or stop after 30 turns and report the remainder
> explicitly.

---

Delete this file when the round lands (plan docs are working documents; `docs/` carries
only settled architecture).
