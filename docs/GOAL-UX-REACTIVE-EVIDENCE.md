# GOAL-UX-REACTIVE — the evidence, in one place

Written 2026-07-27 because the evidence for these twenty conditions kept living in transcript scrollback,
which meant every new context window made it look unproven. It is a *record*, not a claim: every line below
is command output, and every command is named so it can be re-run.

Re-run everything: the commands are inline. Nothing here is asserted; where a fix is claimed, the fix was
broken deliberately and watched to fail first.

---

## 1 — the history endpoint is deployed, not just written

`kubectl exec <catalog pod> -c catalog -- python -c "httpx.get('http://localhost:2333/openapi.json')"`

```
pod: pod/lance-ns-catalog-84874df76c-lbrdr
total paths: 101
history: ['/v1/table/{id}/history']
operationId: table_history_v1_table__id__history_get
```

## 2 — the ingress permits a long-lived stream

`kubectl get ingress -o jsonpath=…`

```
lance-ns-frontend  proxy-read-timeout=3600
```

## 3, 9, 10, 16 — both users, all four zones, the panel scoped to its own dialog

`node scripts/verify_all_zones_both_users.mjs` → **`✓ conditions 9, 10, 16 PROVEN`**

```
✓ alice sees the run bell in /home · /lakehouse · /media · /annotator   — 1 bell(s) each
✓ bob   sees the run bell in /home · /lakehouse · /media · /annotator   — 1 bell(s) each
  /lakehouse/api/runs -> 200, 891 runs, 2 failed
✓ the bell opens a panel with its own role and name
✓ the panel — not the page — says Notifications
✓ a FAILED run is inside the panel, with its error, not just a red badge — maintain: Wrapped error:
✓ failures sort above completions — first state word: Failed
✓ no silently cut text in 12 measured panel rows — 0 cut without a marker
✓ every truncated row carries the full string in title= — 0 unrecoverable
✓ the media zone's bell opens a panel fed by ITS OWN transport
✓ the annotator zone's bell opens a panel fed by ITS OWN transport
  alice → /lakehouse/admin/audit: 200      bob → /lakehouse/admin/audit: 403
✓ bob is refused, and told WHY — 403: "Admin is estate-admin only. These surfaces span every tenant."
✓ and alice, who holds the privilege, gets the surface itself — 200
```

Condition 10's measurement is in the script: element crops at `deviceScaleFactor: 3`, and overflow is
classified rather than eyeballed — a `line-clamp`/ellipsis with a `title` is announced truncation, a bare
overflow is a clipped descender. Two "defects" this caught were **my measurement**, not the product: the
clamped error rows, and a 200-character read of sidebar chrome that hid the words *"Admin is estate-admin
only"*.

## 4 — the timers, and a stated reason for every survivor

```
home 0 · lakehouse 1 · media 1 · annotator 0        (real calls, comments excluded)

lakehouse/src/lib/models/Experiments.svelte:12:   // POLL REASON: a decaying rate has no event…
media/src/lib/service-health.svelte.ts:23:        // POLL REASON: liveness has no event…
```

Enforced by `@repo/zone-contract/poll-reason.test.ts` — a new timer without the marker fails the gate.

## 5 — user work follows the person, not the browser

`node scripts/verify_user_state_browser.mjs` → **`✓ condition 5 PROVEN`**

```
✓ alice sees her new view in the context that saved it — cond5-ms2xy306
✓ the server holds it — HTTP 200
✓ a FRESH browser context shows the view
✓ bob does NOT see alice's view
```

## 6 — the expensive read is cached, shared, and still gated

`node scripts/verify_atlas_cache.mjs` → **`✓ condition 6 PROVEN`**

```
alice cold: 200 miss 6678928B
✓ a repeat read is a hit
✓ a second CALLER is served warm — one fill for everyone allowed to see it — 200 hit 6678928B
✓ an unauthenticated caller is refused WHILE the entry is warm — 401, 29B
✓ and refused on the annotator zone too — gating a URL is not gating a resource — 401, 29B
✓ junk `v` tokens cannot fork the cache — hit,hit,hit,hit,hit
✓ and the product entry survives them — hit
```

## 7 — every gate, cold

```
uv run ruff check services scripts tests          All checks passed!
uv run ruff format --check services scripts tests 368 files already formatted
uvx ty check                                      All checks passed!
uv run pytest tests/unit tests/integration        1213 passed in 30.40s
make openapi                                      no drift
bash scripts/prod_render_check.sh                 ✓ NetworkPolicy=12, OpenFGA=3, Dapr-HA on, PDBs=14,
                                                    spread=7, tiers=3, alerting on, write-cap=2 fits 1Gi,
                                                    rustfs-externalize atomic, ESO path renders
turbo run check test lint fmt:check build --force  Tasks: 43 successful, 43 total · Cached: 0 of 43
turbo run test:e2e                                 home 5 · lakehouse 215 · media 2 · annotator 8
```

**One honest note on the e2e run.** The first attempt reported `16 failed`. Every failure was
`net::ERR_CONNECTION_REFUSED at http://localhost:5294` — I had wiped every `.svelte-kit` for the cold build
while dev servers from the previous run were still alive and being reused (`reuseExistingServer` is on
locally). Killing the stale listener and re-running gave **215 passed, exit 0**. Not a product regression,
and worth recording precisely because "16 failed" is exactly the shape of thing that gets waved away.

## 8 — images rebuilt, pods deleted, digests changed

Zone digests before → after a `make frontend-images && make frontend-load` + `kubectl delete pods`:

```
annotator  33bd12c2 → 4d6f2d90 → 957fb0f4
home       ac8a88d1 → 0aa2e0f6 → a163b388
lakehouse  fbcc366b → 50869fd3 → f7ff40f4
media      598d1eed → 28e69448 → b7368c16     (28e69448 = the id `kind load` reported loading)
```

Read **by container name**, never `containerStatuses[0]` — index 0 is the daprd sidecar on a 2/2 pod, and
its digest is identical across every service. `tests/unit/test_invariants.py` now forbids index access.

## 11 — the svelte MCP autofixer, per touched component

26 `.svelte` files changed since the goal was set. All 26 through `mcp__svelte__svelte-autofixer` at
`desired_svelte_version: 5`. **Twenty returned `{"issues":[],"suggestions":[]}`. Zero issues across all 26.**
Six returned suggestions; each judged in the goal tracker (retired — git history). One was a real defect — `saved-views` called
`load()` inside an `$effect` whose guard reads state `load()` assigns, so two components mounting in the
same tick each issued a full GET. Fixed with an in-flight promise; broken deliberately →
`AssertionError: expected 3 to be 1`.

## 12 — the ledger is current

The parent tracker (`GOAL-VERIFY-PULL.md`) was rewritten row by row against the day's evidence, then
retired with the goal on 2026-07-27 (git history). Open work moved to `OPEN-WORK.md`; the merge state
lives in the rask plan.

## 13 — every open task disposed of

The disposition table — 18 tasks, each done-with-evidence or struck with a stated
reason — lived in the retired goal tracker (git history). The still-open items carry in `OPEN-WORK.md` so they survive the merge.

## 14 — the recorded mistakes have guards, and the guards were broken on purpose

```
=== guard 1: reintroduce | default 1 ===
E   chart/templates/gateway.yaml:92: replicas: {{ .Values.gateway.replicas | default 1 }}
=== guard 2: reintroduce containerStatuses[0] ===
E   scripts/ray_e2e_stack.sh:125: … jsonpath='{.items[0].status.containerStatuses[0].imageID}'
=== guard 3: drop auth.secretStore from the state store ===
E   AssertionError: component lance-statestore uses secretKeyRef with no auth.secretStore, so Dapr
    resolves it from a Kubernetes Secret instead of OpenBao
```

Restored → `pytest tests/unit/test_invariants.py` **34 passed**. Guard 1 then found a live instance of its
own class nobody was looking for: nine `replicas: {{ … | default 1 }}` sites rendering `1` for an explicit
`0`. Proven both ways — `AFTER gateway Deployment -> replicas: 0`, `BEFORE … replicas: 1`, rest of the
render byte-identical.

Two more guards were added later and also broken first:
`notification-surface.test.ts` (*"annotator's root layout renders AppShell WITHOUT a notifications feed"*)
and `no-networkidle.test.ts`. Restored → **591 passed**.

## 15 — a live stream past 255s with no reconnect

`HOLD_S=270 node scripts/verify_live_stream_timeout.mjs`

```
→ alice signs in and opens /lakehouse/admin/events (holding 270s, clearing the 255s bar)
  #1 opened at t+0.8s, STILL OPEN after 270.0s
  #2 opened at t+0.8s, STILL OPEN after 270.0s
✓ no stream was severed during 270s — 2 opened, 0 closed
✓ the live stream survived past 255s
```

The script used to print "past a 60s nginx default" whatever the hold, so a long run proved the harder bar
while labelling itself with the easier one. It now names the bar it actually clears. **Evidence that
mislabels itself is not evidence.**

## 17 — no session-gated 200 says `Cache-Control: public`

`bunx vitest run src/server-cache.test.ts` → **21 passed**, including *"never replays `public` on a response
this route gated behind a session"*. Broken deliberately (the rewrite disabled):

```
AssertionError: expected 'public, max-age=300' to be 'private, max-age=300'   ×2
AssertionError: expected 'max-age=120'        to be 'private, max-age=120'
```

## 18 — the rows/bytes column is honest, never a misleading 0

`bunx playwright test e2e/data/table-history.spec.ts` → **24 passed**, including
*"rows/bytes are shown only where the writer measured them (#113)"* and *"an empty author and a missing run
both read as an explicit dash"*. The empty cell renders `—` with
`title="the run that wrote this version measured no output statistics"`.

## 19 — the data-loss path is closed by a test that fails without the fix

`uv run pytest tests/unit/test_user_state.py` → **35 passed**. With `get()` returning `None` again instead
of raising:

```
FAILED tests/unit/test_user_state.py::test_an_unparseable_record_is_unreadable_not_absent
1 failed, 34 passed
```

Client half: `user-state.test.ts` + `saved-views-store.svelte.test.ts` → 11 passed.

## 20 — workflows use `pipeline()` unless a barrier is justified

```
ux-reactive-track:            parallel=0 pipeline=1     (the one that cost ~40 minutes; rewritten)
discharge-owner-decisions:    parallel=0 pipeline=1
rask-docs-zone-set-sweep:     parallel=0 pipeline=1
frontend-state-architecture:  parallel=1 pipeline=0     ← justified barrier
```

The survivor fans four design agents into a **single** judge that scores them against each other — stage N
genuinely needs all of stage N−1, which is the test the rule turns on.

---

## The closing note worth keeping

Every one of these was green before the drive that found the real defect. The notification bell was shared,
tested and shipped — in **one zone out of four**. Two adversarial passes returned **4/4 REFUTED** on claims
already pushed and green, including an anonymous 6.6 MB read reachable in two zones. Main was red for five
CI runs on two causes invisible to every local gate.

Twenty conditions met is a floor, not a finish.
