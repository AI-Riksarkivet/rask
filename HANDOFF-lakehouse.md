# Handoff — the lakehouse zone

Written 2026-08-06 from the `ingest-plane` session. Everything below is **measured**, with
file:line. None of it is mine to fix — it is the lakehouse's, and it kept surfacing while I worked
next to it.

**Branch state:** `ingest-plane` == `origin/main` at time of writing. Work from a fresh branch off
`main`, not off `ingest-plane`.

---

## 1 · The BFF JSON residual — the last zone not converged

The standing transport ruling (2026-08-03): typed app **values** ride remote functions; **tabular /
binary** rides Arrow IPC or raw bytes on `+server.ts`. Six zones hold **zero** `requestJSON` call
sites — `home`, `explorer`, `annotator`, `compute`, `models`, `studio`. The lakehouse is the only
one left, and `@rask/zone-contract`'s new `transport-contract.test.ts` now pins its count as a
**ceiling**: it may shrink, never grow. Lower the number in the same commit that converges one.

Four real call sites. They are **not** the same job:

| Site | What it is | Verdict |
| --- | --- | --- |
| `lib/storage/storage.ts:69` — `listObjects` | S3 object browser over the `/api/explorer/**` seam | **converge** to a remote `query()` |
| `lib/storage/storage.ts:73` — `headObject` | same seam | **converge** |
| `lib/data/catalog.ts:119` — `fetchTableHistory` | the #113 commit log over the `capi/v1/table/[id]/[...rest]` proxy | **converge** |
| `lib/data/catalog.ts:126` — `insertRows` | POSTs an **Arrow body**, reads only a JSON ack | **keep the route, fix the spelling** |

`insertRows` is the interesting one and the easy mistake: it is a legitimate keep-bytes route that
merely *reads* its acknowledgement as JSON, so it was written with the JSON helper. Converting it to
a remote function would be the mirrored error the ruling warns about — devalue carries an
`ArrayBuffer` as **base64 inside the payload string**, costing +33% on the wire, triple-buffering the
whole body (bytes → base64 → bytes, no streaming), and losing content-type, ETags and ranges. Leave
the transport; give the helper a name that says "bytes out, JSON ack back".

### A trap: `requestJSON` is also a LOCAL name here

`lib/lineage/remote/lineage.remote.ts:60` defines its **own** `requestJSON` inside a remote function
— it reaches the lineage service directly and has nothing to do with the BFF helper. A grep-based
count sees 12 hits in this zone; only 4 are BFF calls. Do not "converge" the local one; it is already
on the right side of the ruling and only the name collides.

**Reference migration:** `lib/admin/remote/access.remote.ts` (the FGA workbench) — queries plus a
write/delete `command()` pair, single-flight `fetchStore().refresh()`, `ApiResult<T>` union returns
(status-driven UI, not exception flow), valibot at the wire boundary, contracts in a sibling
non-remote module because a `.remote.ts` may export only remote functions.

---

## 2 · Two schemas are hand-copied across zones, and both went stale today

`createWarehouse` and `checkAccess` are declared in **both** `home` and `lakehouse`. The per-zone
`command()` shim is structural and cannot move — `query.live`/`command` must be declared inside an
app to get its own endpoint and reach `getRequestEvent`. **The schema is not structural.**

What happened: main's #73 made `protected` a **required** field on `CreateWarehouseBody`. Both
copies went stale. Both broke identically. The same one-line fix landed twice, an hour apart, and the
second only because a typecheck happened to run across both zones.

The failure is also badly located — valibot's inferred argument type silently loses the field, and
TypeScript reports it as *"no overload matches this call"* on `command(...)`, three screens of
overload text naming schema internals, with the actual missing property mentioned only at a **call
site in a different file**.

`transport-contract.test.ts` now fails the moment the two drift, keyed on **name + upstream path** (a
name collision across different APIs is not drift — `createProject` is genuinely two operations). But
the gate only makes it loud. **The fix is hoisting these schemas into `@rask/api`, beside the
generated catalog types they mirror.** `checkAccess` already shows the shape: the lakehouse passes a
named `SubjectSchema` while home inlines the object — name it once, in one place, and import it.

---

## 3 · The zone typechecked red and nobody had run it

`lakehouse:check` had **2 svelte-check errors** before I touched it (`warehouses.remote.ts`,
`WarehouseAdmin.svelte`), and `home` had **3** of the same class. I fixed those to get main green,
but the lesson stands and is worth acting on: `make check` reaches **neither** svelte-check nor the
frontend tests. Run this before declaring a lakehouse change done:

```
bun --cwd=frontend run check test
```

---

## 4 · The zone is the estate's biggest and its `/api` proxy points somewhere surprising

Flagged because it costs an afternoon the first time. Zones disagree about what `/api` means:

- `compute`, `studio`, `models` proxy `/api` → `VIEWER_BACKEND` (`:8888`, the **gateway**)
- `home`, `lakehouse` proxy `/api` → `LANCE_BACKEND` (**`:8001`**, the lineage service — which
  `dev-micro.sh` does not start)

Server-side the same split exists: `compute` reads `RASK_GATEWAY_URL`; `home`/`lakehouse` go through
`makeZoneHooks(env, {gateway:true})`, which reads **`LANCE_GATEWAY_URL`** and defaults to
`http://localhost:8001`. Local dev sets only `RASK_GATEWAY_URL`. **Treat any "works in compute, fails
in lakehouse" SSR fetch as this**, not as a bug in the call.

---

## 5 · Not the lakehouse's, but it renders there — `/api/projects` returns 503

Measured in a browser against the deployed estate:

```
fetch('/api/projects') -> 503 {"detail":"cannot reach kubernetes api"}
```

The controlplane cannot reach the k8s API in-cluster (ServiceAccount / RBAC). Consequence: no active
project can be selected anywhere, and the navbar renders its **placeholder as a link** — text
`"Select project"`, href `/projects/Select project`, literal space.

Two independent fixes, and the second is the lakehouse-adjacent one: the switcher must render a
disabled affordance when the list is empty or unresolved, rather than templating prompt text into a
URL. That is wrong regardless of how the 503 is resolved.

---

## Suggested order

1. **§3** — run `bun --cwd=frontend run check test` and see where the zone actually stands.
2. **§2** — hoist the two shared schemas into `@rask/api`. Small, and it stops a whole class.
3. **§1** — converge the three storage/history call sites; lower the ceiling in
   `transport-contract.test.ts` in the same commit. Leave `insertRows` on its route.
4. **§5 half B** — the placeholder-as-href, which needs nobody else.

§5 half A (the 503) belongs to whoever owns the controlplane's RBAC. §4 is context, not a task —
unless someone wants to reconcile the two `/api` meanings, which is an estate-wide decision.
