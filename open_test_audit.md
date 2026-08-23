# open-test-audit — what the green means, and where there is no green at all

Re-run 2026-08-22 against `47dba152`…`67e7806f`. **113 agents: 12 blind lenses over the whole estate,
one adversarial challenger per finding, then a completeness critic over the planes the lenses admitted
they never entered.** 100 candidates, 100 challenged → **6 refuted, 59 corrected, 35 confirmed as
filed**; the 94 survivors grade **29 HIGH · 33 MEDIUM · 31 LOW · 1 dropped to none**. The challengers
were told to default to REFUTED and to reproduce or kill; most of what survived survived a *mutation
experiment* — the source edited, the suite run, the source restored, `git diff` verified clean — not a
reading. 11.7 M subagent tokens, 4,138 tool calls, 2 h 11 m.

**The v1 audit (2026-08-15) asked one question:** if the implementation were replaced with `pass`,
would this test still be green? That question is still the sharpest one for a single test, and it
found most of what is below.

**It is no longer the first question.** The first question at HEAD is:

> Is this gate running at all — and if it is red, has it been red long enough that nobody reads it?

Because the answer, measured against GitHub Actions rather than inferred, is that **this repository
has not had a green CI run in its last 100 attempts.** Every finding about a subtly weak assertion is
downstream of that — and the completeness critic named it as the cross-lens root cause: twelve lenses
spent their budget asking *"is suite X wired into a CI job?"* and not one asked *"does that job run?"*

---

## GATE STATUS — measured 2026-08-22, every command run UNPIPED

A pipe masks the exit code (`build | tail -1` reports tail's status), and this audit caught itself doing
exactly that once, so each row below is the exit code of the command itself with no pipe in it.

| # | command | result |
| --- | --- | --- |
| 1 | `uv run pytest -q --no-cov -m "not e2e and not slow"` | ✅ **exit 0** — 5202 passed, 7 skipped, 88 deselected, 1 xfailed, 242 s |
| 2 | `bunx turbo --cwd=frontend run check check:tsgo test lint fmt:check --continue` | ✅ **exit 0** — 74/74 tasks, no `Failed:` line |
| 3 | `dagger call charts` | ✅ **exit 0** — 17 steps, 27.4 s (first green since 2026-08-04) |
| 4 | `dagger call frontend` | ✅ **exit 0** — 74/74 tasks, 53 s, 0 cached |
| 5 | `dagger call test` | ⏳ **still running** at 1 h 02 m — H19's NATS binding is unverified and stays uncommitted until it returns |

Gate 2 needed three fixes before it went green, and gate 3 needed five: both were reporting success
while measuring a fraction of what they claimed. Those are recorded at H3 and H22 respectively.

## REMEDIATION TRACKER — keep this current

`open_python-audit.md` shipped 304 findings with **zero** DONE markers and is now 490 commits stale, so
nobody can say what is drained. That failure is cheap to avoid and expensive to inherit. Update this
table in the same commit as the fix; a row without evidence is not done.

### Tier 1 — restore observation (the precondition for everything else)

| | item | finding | state |
| --- | --- | --- | --- |
| 1 | Mock the Dapr proxy in `test_annotation_task_actor.py`; reset the factory cache in `conftest.py` | H1 | ✅ **DONE 2026-08-22** — 62 passed / 0.36 s with the sidecar unreachable (was ~60 min); full suite 4879 passed, exit 0; ruff + `ty` clean |
| 2 | Format the 9 lakehouse `.svelte` files | H3 | ✅ **DONE** — landed by a concurrent session; verified `0 / 160 files` at `e6a6053b` |
| 3 | `--continue` on both turbo invocations | H3 | ✅ **DONE 2026-08-22** — `.dagger/frontend.go:53` + `ci.yml`. Measured: 45/61 tasks and 1 failure → **70/74 tasks and 4 failures** |
| 4 | Commit or delete the `models` e2e harness | v1 H4 | ⬜ **owner decision** — untracked at HEAD; `ci.yml:262-266` documents it as landed |
| 5 | Regenerate `docs/catalog-openapi.json` | M26 | ✅ **NO-OP 2026-08-22** — regenerated at `e6a6053b`, `git diff` empty. The drift was against `47dba152`; a concurrent session fixed it. M26's *structural* half (2 of 10 services; the in-repo guard is name-only) stands |
| 6 | Give `.dagger/charts.go:103` its render arguments | H22 | ✅ **DONE 2026-08-22** — `dagger call charts` **exit 0**, all steps, unpiped (27.4 s, 17 steps). The two follow-ons this row listed as still-red are green too: `make prod-render-check` exit 0 (`NetworkPolicy=12, OpenFGA=3, PDBs=17, spread=11, tiers=3`) and `make alert-rules-check` exit 0 (`20 rules found`) after resolving promtool from PATH-or-`.localbin` rather than assuming a PATH export that `git log -S` proves never existed. Running the repaired gates surfaced 6 further defects, all fixed — including **2 gates matching YAML COMMENTS rather than config** (the M8 class, live) and, once the render worked, L6's latent absence-assertion vacuity becoming reachable |
| 7 | `assert_all_called=True` on respx | M19b | ✅ **ENFORCED 2026-08-22** — flipped on the global router in `conftest.py`, so it covers all 118 bare `@respx.mock` sites and every future one. **17 of 227 went red**, and the split is the point: 13 were DEAD routes in the register path (M19 — a top-level namespace-create the cascade is ruled never to make, mocked as 200 where the real catalog answers 400), now deleted; 6 were NEGATIVE routes whose uncalled state is the assertion, now declaring themselves via a `respx_allows_unused_routes` fixture. Dead and deliberate are no longer indistinguishable in the source |
| 8 | Drive the `--continue` gate to GREEN — fix what it unmasked | H3 | ✅ **DONE 2026-08-22** — **74/74 tasks, exit 0, unpiped.** `--continue` turned 1 reported failure into 4; all three newly-visible ones are fixed, and every one was a committed file no session had modified — i.e. defects the fail-fast gate had been hiding, not fresh breakage. (a) `explorer#check:tsgo`, 5 errors: `tsconfig.tsgo.json`'s `include` **replaces** the parent's instead of merging, silently dropping `.svelte-kit/{ambient,env,non-ambient}.d.ts` + `src/app.d.ts`, so `$env/dynamic/private` and the `App.Locals` augmentation had no declarations — 4 config errors wearing the costume of 5 code errors; the 5th was real: `v.optional()` widens a key to `boolean \| undefined`, which `exactOptionalPropertyTypes` refuses, fixed with valibot's purpose-built `v.exactOptional()` rather than by loosening `UserStateEnvelope`. (b) `@rask/zone-contract#check:tsgo`: an unguarded regex capture under `noUncheckedIndexedAccess` — one guard also removed the `as string` the next line used to paper over the same value. (c) `@rask/ui#fmt:check`: 3 files, and `grants-panel.svelte` was the interesting one — `rsvelte-fmt` reformats a multi-statement arrow inside a markup attribute to column 0, and the reason one was there at all is that a `// #72` comment in the `<script>` block had been describing a handler that was moved into the markup, leaving the comment documenting nothing. Naming the handler fixed the orphaned comment and the formatter's bad output together; MCP autofixer clean |

### Tier 2 — after CI is green

| | item | finding |
| --- | --- | --- |
| 9 | Identify the **`F` at 41 %** on the first green `ms-test` | H1 (open thread) |
| 10 | Drain-state pass over `open_python-audit.md`, then **merge** into one backlog | Part 13 |
| 11 | Then its waves: E1/E2 → E3/E4 → E5/E6 → E7/E8/E10/E11 → E12 — guard clauses and deletions **last** | that audit's own execution order |

*Not tracked here:* `TODO.md` at repo root is the product/feature backlog and a different list entirely.

---

## Part 0 — There is no green gate

```
$ gh run list --workflow=ci.yml --limit 100 --json conclusion
     65 cancelled
     35 failure
```

Zero success. The oldest run the API still serves is 2026-08-16; on every run I sampled between then
and 2026-08-22 — seven distinct commits — `ms-test`, `ms-charts` and `web-gate` are all `failure`.

The job graph turns that into something worse than "some jobs are red":

| job | 2026-08-22 | why |
| --- | --- | --- |
| `ms-test` | **failure** | one non-hermetic unit test, **H1** |
| `ms-charts` | **failure** | renders a config the chart refuses — and dies before five more gates, **H22** |
| `ms-openapi` | **failure** | committed spec drifted from generated, **M26** |
| `web-gate` | **failure** | `lakehouse#fmt:check` → fail-fast, **H3** |
| `web-e2e` | **failure** | `models#test:e2e` → `Error: No tests found` (v1's **H4**) |
| `supply-chain-secrets` | **failure** | 167 "verified" trufflehog hits, **§0.1** |
| `e2e-lineage` | **skipped** | `needs: ms-test` |
| `e2e-stack` | **skipped** | `needs: ms-test` |
| `e2e-ray` | **skipped** | `needs: ms-test` |
| `e2e-auth` | **skipped** | `needs: ms-test` |
| `web-smoke` | skipped on schedule | `if: push \|\| same-repo PR` |
| `ms-authz`, `ms-lint`, `ms-typecheck` | success | |

**Four live lanes have not executed in at least six days.** The governed kind stack, the Ray path, the
AGE lineage graph and the Dex/OpenFGA auth chain all hang off `needs: ms-test`, and `ms-test` fails
on one test file. v1's H1 said "11 suites run by nobody." The truth at HEAD is that the suites which
*are* wired into a lane also run by nobody, because their lane never starts.

This reframes every "green" claim in the repository. `make test` passing on a developer box is not
the same statement as `dagger call test` passing in CI — H1 is precisely a case where those two
diverge by design.

### 0.1 — `supply-chain-secrets` blocks on 167 "verified" hits, and one of them is a test name

Unlike `supply-chain` and `supply-chain-images` (both `continue-on-error: true`, deliberately and
documented as temporary at `ci.yml:101-116`), **`supply-chain-secrets` is blocking** and exits 183.

```
✅ Found verified result 🐷🔑
Raw result: test_client_error_wraps_to_storage_error
…
"verified_secrets": 167, "unverified_secrets": 0
```

`test_client_error_wraps_to_storage_error` is a **pytest function name**. The gate's whole design
premise — recorded in CLAUDE.md as "gates on VERIFIED credentials only" — is that *verified* means
real. A run reporting 167 verified secrets, at least one of which is an identifier, has broken that
premise. Whether the remaining 166 are false positives or a genuine leak, the operational answer is
the same and it is the bad one: **a real credential landing tomorrow is indistinguishable from the
noise**, because the gate has been red and unread for days.

*Not classified here.* This needs its owner to triage the 167, not an auditor to guess. It is in this
document because a permanently-red security gate is a test-estate defect regardless of which way the
triage goes.

---

## Part 1 — One unit test stops the offline suite, and stopping it hides everything after

### H1 — `tests/unit/test_annotation_task_actor.py` talks to a real Dapr sidecar · **CONFIRMED, HIGH**

✅ **FIXED 2026-08-22** — landed in `493f8e06` (the suite) and `5b5e2a39` (the estate-wide guard), and
tracked as Tier-1 row 1 above with the measurements. In short: the file went from ~60 minutes to
**0.36 s / 62 passed** with the sidecar unreachable, and the root `conftest.py` now clears
`ActorProxy._default_proxy_factory` before each test so one test can no longer decide another's
outcome. The full offline suite runs 4879 passed, exit 0; ruff and `ty` clean.

This is the single highest-value finding in the audit. It was found independently by two lenses and
by the main loop, and every number below was measured.

**The path.** `actor.fire(...)` → `annotator/projects/actor.py:307 _report_state` →
`proxies.py:98 typed_proxy` → the real `ActorProxy.create` → `dapr/clients/http/client.py:54
DaprHttpClient.__init__` → **`DaprHealth.wait_for_sidecar()`**, which is a `time.sleep` loop bounded
by `DAPR_HEALTH_TIMEOUT` (Dapr's default: 60 s).

**Why it is invisible.** `_report_state` is wrapped in `except Exception` and is non-fatal *on
purpose* — its docstring explains why, and the reasoning is correct. So the tests cannot fail because
of the sidecar. They can only **wait** for it.

**Why the wait never ends.** `dapr/actor/client/proxy.py:135-138` assigns
`cls._default_proxy_factory = ActorProxyFactory()` — the cache is populated *after* the constructor
returns. When the constructor raises, nothing is cached, so **every subsequent call re-waits the full
timeout**.

**Measured, on this box:**

| condition | result |
| --- | --- |
| a Dapr sidecar answering `127.0.0.1:3500/v1.0/healthz` → `204` | **61 passed in 0.49 s** |
| `DAPR_HTTP_PORT=9`, `DAPR_HEALTH_TIMEOUT=3` | **61 passed in 285.54 s** |
| CI (nothing listening, Dapr default 60 s) | ~60× the above |

A **580× slowdown, and all 61 still pass.** That is the shape of the defect: the suite's *correctness*
is unaffected by the sidecar and its *runtime* is a function of it.

**What that does to CI**, read out of the `ms-test` log for run `32548501993`:

```
[ 4m1s] ........ [ 40%]
[ 5m2s] ....F... [ 41%]      <-- a real failure, never reported
[26m2s] ........ [ 44%]      <-- a 21-minute stall
[45m2s] ......+++ Timeout +++
Health check on http://127.0.0.1:3500/v1.0/healthz/outbound failed: [Errno 111] Connection refused
```

`--timeout=300 --timeout-method=thread` fires and kills the process. **56 % of the offline suite never
executes in CI**, and the `F` at 41 % has never been seen by anyone, because the run dies before the
summary line.

**The blast radius is exactly one file.** I timed all 31 test files that reference `typed_proxy` /
`ActorProxy` / `DaprClient`, with and without a reachable sidecar:

```
tests/unit/test_annotation_task_actor.py      up= 2.6s  dead=97.8s   <-- SIDECAR-BOUND
(the other 30)                                up≈ 2-8s  dead≈ same
```

Thirty of thirty-one mock the proxy correctly. The estate's own convention is right there; one file
departs from it.

**Three separate harms, one cause:**

1. `ms-test` red → four live lanes skipped (Part 0).
2. 56 % of the offline suite unrun in CI, an unidentified `F` among it.
3. **A cross-suite ordering coupling.** `services/notifications/tests/test_adversarial_inbox.py`
   asserts a `TimeoutError` from an inbox open that blocks on the sidecar handshake. It passes under
   `make test` *only because* `tests/unit` ran first and left `ActorProxy._default_proxy_factory`
   populated. Run it alone and it fails `DID NOT RAISE`. Its green is an accident of `testpaths`
   order.

**Fix:** mock `typed_proxy` in that file's fixtures, the way the other thirty do. One file. It
unblocks four CI lanes.

> `.dagger/test.go`'s timeout comment says the thread method exists so "the next occurrence names
> itself." It did name itself, correctly, four nights running. The gap was never the instrumentation.

**CLOSED 2026-08-22 — and the ordering half was closed with it.** Two files, 108 insertions, **zero
production lines touched**.

*RED first.* `test_a_transition_builds_NO_real_dapr_proxy_so_this_file_needs_no_sidecar` probes the
factory rather than the timing, because timing is flaky and nothing ever *fails*: it clears the class
cache, drives a plain transition, and asserts `ActorProxyFactory.__init__` was never entered. Red at
HEAD in 0.25 s with `assert not [<ActorProxyFactory object …>]`.

*Fix.* A module-scoped **autouse** fixture stubbing `ActorProxy.create` — the file's own convention,
which two of its tests already used and fifty-nine did not. Autouse rather than per-test precisely
because opting in test-by-test is what let fifty-nine drift. Both tests that assert on what the
project receives still override it and still pass.

*Measured after:*

| | before | after |
| --- | --- | --- |
| sidecar reachable | 61 passed / 0.49 s | **62 passed / 0.42 s** |
| sidecar unreachable, `DAPR_HEALTH_TIMEOUT=60` (CI's default) | ~60 min, killed at 44 % | **62 passed / 0.36 s** |

The environment-dependence is gone outright, not reduced.

*Harm 3 fixed at the harness.* `conftest.py` gains an autouse fixture clearing
`ActorProxy._default_proxy_factory` before each test, so no test's result depends on whether another
warmed the cache. Verified in **both** orders: `tests/unit` then notifications, and the reverse —
98 passed each way. Deliberately **not** a global stub of `wait_for_sidecar`: the adversarial inbox
test exists to prove that handshake blocks the event loop, and stubbing it estate-wide would delete
the only evidence the estate has of a live production defect.

*Whole-estate verification.* Full offline suite under CI's exact selection and timeout — it now
**completes** rather than dying at 44 %. `uv run ruff check .` clean repo-wide, `ruff format --check`
clean, `uvx ty check` clean on both files.

*Two corrections to this audit, from doing the work.*

**(a) I walked into this document's own §Part 12.** The first verification run reported `exit=0`
because it was piped to `tail` — the exact trap `reference-pipe-masks-exit-code` records and that
this document criticises `.dagger/charts.go` for. Unpiped, the suite reports its real result. The
lesson generalises: *the pipe hid a failure from me in the same session I filed the finding.*

**(b) The `F` at 41 % is still unidentified, and neither failure set I saw is it.** Every failure
observed while verifying traced to something other than the code under test:

| observed | cause | verdict |
| --- | --- | --- |
| `test_dapr_disabled_by_default`, `test_registering_an_actor_type_proves_nothing_about_a_SIDECAR` | my own `DAPR_HTTP_PORT=9` injection | both pass under a normal environment — artefacts of the probe |
| 6 × `services/annotator/tests/test_publish_names_the_requester.py` | a concurrent session's **untracked** file (`git status` → `??`), whose parent commit `62bdc571` landed at 13:50 while the run spanned 13:51–13:55 | passes in isolation with *and* without this change; reverting this change to HEAD and re-running it gives 6 passed — not a landed regression |

So the real `F` needs a CI run to name, and this fix is the precondition for getting one. **Do not
close H1's "unidentified `F`" on the strength of this local verification** — it is a separate open
thread, and it is the reason to watch the first green `ms-test` closely rather than assume it.

### H2 — Coverage is computed on every run and gated by nothing · **CONFIRMED, LOW (cost), MEDIUM (blindness)**

`addopts = "--cov --cov-report=term-missing:skip-covered"` applies to **every** pytest invocation in
the estate. There is **no `fail_under`, no `--cov-report=xml`, no artifact upload, no threshold in
`ci.yml` or `.dagger/`** — verified by grep across all five. The measurement costs roughly 60 % extra
wall clock on the one CI Python lane and is written into Dagger's buffered stdout, which nothing reads.

Worse, the denominator is wrong in two ways:

- `source = ["packages/", "services/"]` (`pyproject.toml:232`) names two directories that are **not
  importable packages**, so coverage.py's unexecuted-file discovery prunes the tree: **4,262 of 27,941
  statements never enter the denominator at all**. The headline number describes only files some test
  happened to import.
- `omit = [..., "**/__init__.py"]` (`:233`) was written for empty package markers but deletes **409
  statements of real implementation across 69 files** — including **the entire gateway service**,
  whose whole implementation (route table, `_CLIENT_SPOOFABLE` header strip, the proxy) lives in
  `__init__.py`. The estate's front door is absent from its own coverage report.

✅ **FIXED + ENFORCED 2026-08-22.** All three, each measured rather than reasoned.

| defect | measured before | after |
| --- | --- | --- |
| `--cov` on every invocation, gated by nothing | **10 883 ms** vs 3 990 ms on one subset — **2.7×**, not the ~60 % filed | removed from `addopts`; `make coverage` computes it on request |
| `source` names the workspace dirs, not the src roots | `packages/tracker/tests` discovered **3 files** | **427 files** |
| `omit` deletes every `__init__.py` | the gateway absent from its OWN test run's report | `gateway/__init__.py  140  23  83%` |

The gateway figure matches the finding's "140 statements with 23 uncovered" exactly.

**No `fail_under` was invented.** The plumbing is fixed and the number is honest; choosing a threshold
is an owner's decision, and one picked by whoever repaired the denominator would just ratchet to
whatever happened to be true this afternoon.

Enforced by `tests/unit/test_coverage_denominator.py`: `source` is checked against the same
`packages/*/src` + `services/*/src` globs the workspace uses, `**/__init__.py` may not return to `omit`,
and `--cov` may not return to `addopts`. The first closes an asymmetry `rask-architecture` names —
workspace membership IS globbed, so a new member joins silently, while `source` must be enumerated
because coverage.py cannot glob it. Without the gate a new package would fall out of the denominator as
quietly as it joined the workspace.

**A defect in my own gate, found by this change.** `test_no_docker.py`'s roster keyed violations by
`file:line`, and adding the `coverage` target moved `Makefile:161` — so it failed for a reason having
nothing to do with docker. That is the cry-wolf failure the same file argues against elsewhere (it is
why `docker inspect` is deliberately not flagged). Rekeyed to file + sub-command: stable under
refactors, still fires on a genuinely new site (RED re-proven with a `docker run` planted in
`scripts/k3s-install.sh`).

---

## Part 2 — The frontend gate runs one of thirteen test suites

### H3 — no `--continue` anywhere, and a formatting miss cancels the estate · **CONFIRMED**

`.dagger/frontend.go:53` is `bunx turbo run check check:tsgo test lint fmt:check`. Turbo fail-fasts by
default. **`--continue` appears nowhere in `.dagger/`, the `Makefile`, `frontend/package.json`,
`frontend/turbo.json` or `.github/workflows/`** — every turbo invocation in the repository, CI's and
`make frontend-check`'s alike, is fail-fast.

At HEAD, `lakehouse#fmt:check` fails on nine `.svelte` files. Turbo's own summary from the CI log:

```
 Tasks:    10 successful, 35 total
Cached:    0 cached, 35 total
  Time:    3.125s
Failed:    lakehouse#fmt:check
```

**25 of 35 tasks never ran.** Of every `test` task in the workspace, exactly one reported:
`@rask/api:test`. The `@rask/zone-contract` estate-shape suite — 21 files, the gates on nav truth,
cross-zone reload, transport contracts, dock reachability, the manifest — **did not execute.** Neither
did `@rask/ui`'s, nor any zone's.

So `web-gate` has said "the frontend is broken" for four nights while measuring about a thirteenth of
what its name implies. The trivial half of the fix (`rsvelte-fmt .` on the lakehouse) is not the
interesting half; the interesting half is that a three-second formatting check is currently load-bearing
for the entire JS gate.


**FIXED 2026-08-22** (commit `d1fa2dd4`, tracker row 3). `--continue` added to both turbo invocations. Measured: 45 of 61 tasks and 1 failure reported → **70 of 74 and 4**. Three of the four real failures were invisible, including `@rask/api#test` collecting zero tests.
### H4 — `@rask/api`'s liveness test has never run a single assertion · **CONFIRMED, MEDIUM**

`frontend/packages/api/src/live.svelte.ts:1` imports `svelte`, which `@rask/api` **has never declared
in any dependency field**. `live.test.ts` therefore fails at module load with **0 tests collected** —
and has done since it landed on 2026-08-05. It also takes `web-gate` down with it, which is how it
stayed invisible: the job was already red.


**FIXED 2026-08-22.** `@rask/api` now declares `svelte` — `devDependencies: ^5.56.8` + `peerDependencies: ^5.0.0`, matching the convention `@rask/ui`, `@rask/dockview` and `@rask/flow` already use. bun installs in ISOLATED mode, so `packages/api/node_modules` held only its declared deps and `svelte` was not hoisted to `frontend/node_modules` — hence `Cannot find package 'svelte' imported from src/live.svelte.ts` and `src/live.test.ts (0 test)`. **Measured: 11 passed + 1 failed / 89 tests → 12 passed / 99 tests.** Those 10 assertions had not run since 2026-08-05.
### H5 — `docs-roster.test.ts` cannot execute in the container it ships in · **CONFIRMED, MEDIUM**

`frontend/packages/zone-contract/src/docs-roster.test.ts:27` shells out to `git ls-files` to derive
the zone roster. The frontend gate runs in `oven/bun:1.3.14-slim`, which **has no `git` binary**, and
`.dagger/frontend.go` additionally strips `.git` from the copied source. **4 of its 6 tests cannot run
in CI at all** — they pass locally and are structurally absent from the gate.

---


**FIXED 2026-08-22.** `docs-roster.test.ts` no longer shells out to `git ls-files` (verified: no `execFileSync`, no `child_process`, no `'git'` left in the file). It derives the roster from `zoneDirs()` — a directory under `microfrontends/` carrying a `package.json`. That is not merely the git-free substitute but the better definition: the reason it reached for git was to exclude untracked build residue, and residue carries no `package.json`, which is exactly why bun's workspace glob skips it silently. One behavioural change, stated in the file rather than hidden: a scaffolded-but-uncommitted zone now counts — which is the answer R15 wants ("regardless of scaffold status"). 6 passed; 1,256 across zone-contract's 22 files.
## Part 3 — The authorization model's own tests cannot see a widening

This is the most serious *coverage* cluster, as opposed to the *execution* cluster above. All four
were proved by mutation against `fga model test` and the full offline suite.

### H6 — 22 of 60 `can_*` derivations can be widened with 44/44 model tests green · **CONFIRMED, HIGH**

`packages/service-kit/.../auth/model.fga.yaml:12`. Of 73 `can_*` relations, 13 are no-op mutants
(their lowest rung is already a top-level OR term), leaving 60 meaningfully widenable. **22 of those
60 can each be widened to "anyone holding the object's LOWEST rung" and `fga model test` still reports
44/44 tests, 266/266 checks passing.**

A permission that quietly gains `reader` is not a syntax error and no assertion in the file is shaped
to notice. The model tests pin *type names and a sample of allow/deny rows*; they do not pin the
derivations.


**ENFORCED 2026-08-22.** `packages/service-kit/src/service_kit/governed/auth/model.fga.yaml`. Method: a mutation SWEEP — widen every `can_*` to its type's lowest rung, re-run `fga model test`, record which survive. 15 survived silently after the table fix (22 at audit time). All 15 now carry a negative assertion against a subject that HOLDS the low rung, with a liveness line so the negative cannot go vacuous. **Re-swept: 62 widenable derivations tested, 0 still unpinned.** RED/GREEN proof: `table.can_drop: owner -> reader` gave 44/44 + 266/266 before, and `43/44 + 274/275` after, naming `Check(user=user:peter,relation=can_drop,object=table:acme_gold_catalog): expected=false, got=true`. Checks 266 -> 296.
### H7 — `NOTIFY_RELATION` can be replaced with a relation that does not exist · **CONFIRMED, HIGH**

`services/notifications/src/notifications/api/visibility.py:60`. Mutating it to `can_be_notifiedX` —
a relation the model does not define — leaves **769 tests passing, 0 failing** across
`services/notifications/tests`, `test_invariants.py` and `test_fga_model_contract.py`.

In production that fails closed and **silences the entire inbox plane**. The relation that gates every
notification delivery in the estate is guarded by nothing.


**ENFORCED 2026-08-22.** `tests/unit/test_invariants.py::test_every_relation_constant_names_a_relation_the_model_defines`. RED/GREEN: setting `NOTIFY_RELATION = "can_be_notifiedX"` gave **774 passed** before, and now fails naming `services/notifications/.../visibility.py:60 -> NOTIFY_RELATION = 'can_be_notifiedX'`. The gate scans the CONSTANT rather than its call sites, because `NOTIFY_RELATION` is passed POSITIONALLY (`self._filter(subject, names, NOTIFY_RELATION)`) — no `relation=` kwarg exists to pair with an object literal.
### H8 — the production `lockRootCreate` posture is untested · **CONFIRMED, HIGH**

`services/catalog/src/catalog/api/fga_deps.py:273-274`. `chart/values-prod.yaml:22` ships
`lockRootCreate: true` — the only thing stopping any authenticated token from minting top-level
namespaces and tables in production. That branch can be **silently downgraded from the writer-tier
`can_create_*` to the reader-tier `can_get_metadata`** and the whole catalog + unit + integration
suite stays green (**3,001 passed**).

v1's H2 established that the estate has two postures, one per environment. It fixed the test to assert
the *dev* posture. **Nothing asserts the prod one.**


**ENFORCED 2026-08-22.** `tests/unit/test_fga_model_contract.py`. The existing assertion checked that the locked-root pair RESOLVES in the model, never which TIER it is — and `can_get_metadata` resolves on the root type perfectly well, which is why the downgrade was invisible. It now asserts the locked-root relation EQUALS the nested one: `lockRootCreate` moves WHERE the check happens, never WHICH permission it demands. RED/GREEN: the mutant (`return settings.fga_root_object, "can_get_metadata"`) gave **3,125 passed** before, and now fails with *"the locked-root create for 'table' asks for 'can_get_metadata' while the nested create asks for 'can_create_table' — locking the root must not downgrade the tier it demands"*.
### H9 — the phantom-relation scanner reaches 10 of the estate's relations · **CONFIRMED, raised to HIGH**

`tests/unit/test_invariants.py:316-342`. `_fga_literals()` is the estate's only repo-wide guard against
a service checking a relation the model does not define. Its regexes are literal-only and
whitespace-sensitive, so across all of `services/` it resolves **10 distinct (type, relation) pairs —
zero from lineage, notifications, viewer or flows.** Proved by full-suite mutation: a phantom on the
notifications delivery gate leaves **4,849 tests green**. This is the mechanism behind H7.


**PARTIALLY ENFORCED 2026-08-22 — stated honestly.** The new gate above closes the CONSTANT class, which is where the estate's load-bearing relations actually live: 9 constants across notifications, annotator and viewer, all now validated against `model.json`. It reaches the viewer's `READ_DATA`/`READ_METADATA`/`BROWSE_STORAGE` — which carry no `_RELATION` suffix and are declared in `api/security.py` but used as `relation=READ_DATA` from endpoint modules, so a same-file scan found none of them. **Still open:** `_fga_literals()`'s own bounded-window pairing is unchanged, so a relation LITERAL more than four lines from its object literal is still unseen, and a constant imported across a module boundary and passed on is out of reach without dataflow. Both are stated in the gate's docstring rather than papered over.
### H10 — the `table` rung's entire grant axis has no assertion · **CONFIRMED, raised to HIGH**

`model.fga:362-382`. Six `can_*` relations carry no assertion of any kind in `model.fga.yaml`; five of
them are the **`table` rung's whole grant axis** — the finest-grained and by far the most numerous
governed object in the estate. The challenger raised this from MEDIUM on reachability grounds:
`_GRANTABLE_BASE` in `access.py:82` makes every one of them reachable from a real grant call.


**ENFORCED 2026-08-22.** Same commit. The `table` rung's five grant relations (`can_grant_owner/writer/reader/validator`, `can_grant_pass_grants`) had no assertion of any kind; all are now pinned against `user:peter`, a writer without `pass_grants`. Covered by the sweep above, which reports 0 unpinned.
### M1 — `fga model test` runs in exactly one place, and no `make` target is it · **CONFIRMED, raised to MEDIUM**

The OpenFGA model's evaluation semantics are guarded by a single line: `ci.yml:208`. `make ci` (=
`check` + `test`) can be fully green on a machine where the authorization model's own suite has never
executed. Given Part 0, "runs only in CI" is currently a synonym for "runs when ms-authz happens to be
one of the four green jobs."

✅ **FIXED 2026-08-22** — `make fga-test`, wired into `make check` so the default developer gate covers
it. `fga` is already pinned into `.localbin` by `make bootstrap`, so the target needs no new tooling;
it resolves the binary from PATH or `.localbin` the same way `alert-rules-check` now resolves promtool.

**It carries BOTH halves of that CI step, and the second is the one that makes the first mean
anything.** The model exists in three copies and only one is what the app loads: `model.fga` is
authored, `model.fga.yaml` is what `fga model test` evaluates, and `model.json` is what the service
reads at runtime. Testing the yaml while shipping the json is how a tested model and a deployed model
drift apart in silence — so the target also transforms the authored `.fga` and diffs it against the
committed `.json`.

Verified both ways: `make fga-test` exits 0 and reports 45 tests / 296 checks / 8 ListObjects / 2
ListUsers. RED — editing `schema_version` in `model.json` exits **2** with `model.json drifted from
model.fga` and the exact diff.

Scope stated: this does not remove the CI copy at `ci.yml:208`, so the two definitions now coexist. CI
should call the make target rather than repeat it, but `.github/workflows/ci.yml` is held by a
concurrent session in this tree and committing its hunks alongside mine is forbidden here.

### M2 — `fga.list_objects` is exercised by no test · **CONFIRMED**

`model.fga.yaml:450`. `list_objects` builds the allow-list behind every table listing in the catalog —
it is the control that prevents cross-tenant table disclosure. It has **no `list_objects` row in the
model tests and no unmocked Python test anywhere.** The recursive upward-visibility edge the warehouse
listing actually enumerates is covered by neither of the two rows that do exist.

✅ **ENFORCED 2026-08-22** — both halves, conforming to `openfga/references/test-list-objects.md`
(`list_objects` with a `type` and per-relation `assertions`, including the empty-list form it prescribes
for negative coverage).

*The model.* `ListObjects` assertions went **2 → 8**. The two that existed enumerate warehouses and
materialized views; there was no row over `type: table` at all, which is the object the disclosure
control actually guards. A `check` cannot stand in for it: check answers *may this principal read THIS
table*, and disclosure is about what comes back when nobody named a table. Added: `ivan` (one gold
table), `dave` (the recursive upward edge a listing walks — four tables), `quilla` (the beta tenant),
`eve` (`[]`, the empty list IS the assertion). RED proof: adding
`user:dave → reader → table:beta_locked_records` fails naming the leaked table.

**The model corrected me, and the correction is the better assertion.** The warehouse row's own name
claims "only acme, by isolation" while asserting only the one bucket dave sees — which does not prove
another tenant's is excluded unless something enumerates from the other side. I added
`quilla → [warehouse:beta_bucket]` and the model answered `[]`: visibility cascades DOWN the hierarchy
and never UP, so quilla's grant on `table:beta_locked_records` does not make the parent bucket
enumerable. Pinned as `[]` with the reason, because it separates two different facts wearing one result
— *quilla cannot see acme* (isolation) and *quilla cannot see the bucket her own table lives in* (the
cascade's direction) — and only the first would survive someone adding a convenience edge upward.

*The Python wrapper.* `test_list_objects_fails_closed_on_network_error` already existed, so the
outage half was covered. The two that were not: the `qualify` hatch and the condition context, both
added to `tests/unit/test_fga_resilience.py` (20 passed). The listing door's failure mode is quieter
than `check`'s in both cases and that is the point — a double-prefixed `user:user:alice` denies every
check LOUDLY but returns an EMPTY LIST here, which renders as "you have no tables"; and a dropped clock
returns a SHORTER list, where nothing about a shorter list looks wrong. RED: removing the hatch fails 2
tests; sending `context=None` fails the clock test by name.

---

## Part 4 — The notification traps are unguarded at the producer

`.claude/skills/rask-notifications` names four "silent-drop traps": a state change that names nobody is
not under-delivered, it is *undeliverable*, and `notifiable()` acks it **SUCCESS**. So a producer that
drops a field fails silently and is reported by nothing. The lens mutation-tested each trap at the
producer.

**The good news first, because it is load-bearing:** the cascade head is genuinely guarded. Deleting
the bronze-write emit from `services/medallion/services/produce.py` reds **8 tests**; deleting the
`medallion.bronze` publish from `ingest_trigger.py` reds **9**. I reproduced the first myself. CLAUDE.md
calls that emit the thing whose loss means "the whole bronze→silver→gold run silently never happens" —
it is properly defended.

Everything else on the producer side is not.

### H11 — the mover can stop stamping `lance.project` and 4,853 tests pass · **CONFIRMED, HIGH**

`services/medallion/src/medallion/services/transform.py:549`. The challenger ran the **whole offline
root suite** (all 20 testpaths) with the mover's tenant stamp deleted: `4,853 passed, 6 skipped, 1
xfailed`, **zero failures**.

`notifications/api/fanout.py:88` skips the watcher loop entirely when `project` is `None`, *and the run
is still delivered to its author* — so the event looks completely healthy and simply reaches fewer
people. Trap 3, exactly as the skill describes it, unguarded on the shipped cascade path.

The test that looks like it covers this —
`test_medallion_cascade.py::test_project_cascade_routes_into_the_project_warehouse_and_qualifies_lineage`
— asserts the *routing*, not the *stamp*.


**ENFORCED 2026-08-22.** `services/medallion/tests/test_producer_targeting_contract.py::test_every_lineage_emit_stamps_lance_project` — DERIVED over every `build_run_event(` call under `services/medallion` (13 sites), not a listed set, so a new emit site is covered on the day it lands. RED: deleting the mover's stamp fails naming `services/transform.py`. **The derivation also found a site the audit did not have** — `services/media_produce.py` stamps no project — and it is EXEMPT with a reason rather than patched: `ingest_media` takes no project because the media head writes to a configured platform target, and `rask-notifications` records that the door has no `?project=` *"because the media head's target is configured and authorization scope must equal write scope"*. Adding one to reach WATCH would break that invariant, so that lane reaches its author and no watchers BY CONSTRUCTION. Exemptions live in `_PROJECTLESS_EMITS` with the justification inline.
### H12 — `POST /produce`'s 503 tail is executed by no test · **CONFIRMED, HIGH, and understated**

`services/medallion/src/medallion/api/produce.py:77-90`. The route's own docstring makes the contract
load-bearing verbatim: the bronze-write emit is the cascade head, "so a publish failure surfaces as
**503** (not the 202 that would hide it), letting the caller retry."

Line coverage: `77-90` missing. The `publish_failed` check, the 503 problem+json with `Retry-After: 5`,
and the 202 return **never execute**. The one test that reaches the route asserts only
`status_code != 403` against a handler deliberately wired to blow up.

The challenger found it is **three routes, not one** — the identical unguarded 503 branch sits at
`ingest_media.py:64-75`, whose entire handler body is at 46 % with `37-76` missing.


**ENFORCED 2026-08-22.** Same file. Two tests drive the real route function with `run_produce` patched: a `publish_failed` result must answer **503** with `Retry-After: 5` and `application/problem+json`, and its twin asserts a successful produce still returns the 202 body — so the 503 assertion cannot pass by rejecting everything. RED: making the `publish_failed` branch unreachable fails with *"a dropped cascade head must not answer 202 — the run silently never happens"*. The whole branch was previously reported missing by line coverage.
### M3 — the mover's `originator` is guarded by nothing · **CONFIRMED, MEDIUM**

`transform.py:550, 623, 651, 686, 725, 773` and `:802` — seven sites, not six. All can be deleted with
no attributable failure (the challenger ran a clean baseline of the identical command to prove the one
red was pre-existing — the ordering flake from §H1.3).

Why it matters: the mover authors with a **chart role literal** (`MEDALLION_AUTHOR` = `data_eng` /
`analyst` / `ray`), so `author_subject()` addresses an inbox actor named `data_eng` — nobody.
`lance.originator`, carried from `/produce`'s verified sub through `/bronze-arrival`, is the **only**
way a failed cascade run reaches the human who started it. That is trap 2, and nothing holds it.

✅ **ENFORCED 2026-08-22** — premise re-measured first: deleting one `originator=` stamp from
`transform.py` left **217 medallion tests passing**, so the field was held by nothing, exactly as filed.

Gate: `test_every_published_lineage_emit_stamps_originator` in
`services/medallion/tests/test_producer_targeting_contract.py`, beside the `lance.project` gate and in
the same exemption-with-a-reason shape. It walks the AST for every `build_run_event(` call under
`medallion/` and requires an `originator=` keyword. RED with the stamp deleted (**exit 1**, naming
`services/transform.py`); GREEN with it (**exit 0**).

The scan found a **tenth site the finding did not name** — `services/promotion.py:50` — and it is
legitimately exempt, which is worth recording because it is the distinction the gate has to make:
`promotion_lineage` never PUBLISHES its event, it projects one into the `LineageDoc` written beside the
dataset, so it never reaches `notifiable()` and has no audience to target. A provenance document answers
*what produced this dataset*, which is not *who should hear about it*. Exempted with that reason rather
than by narrowing the scan, so the claim stays visible.

Non-vacuity is about REACH, not count: an exemption list is only as honest as the scan feeding it, and a
walk that stopped resolving files would report zero unstamped sites and read as a fully-targeted estate.
`test_the_targeting_scan_sees_every_hop_of_the_cascade` pins that the head, the movers and the workflow
are all still reached, and that the mover module carries several emits rather than one.

### M4 — `request_approval` is executed by no test · **CONFIRMED**

`services/medallion/src/medallion/workflow.py:816-842`. The sole producer of
`promotion_review_requested` — the reason that asks a named person to decide a held promotion — never
runs: the `CatalogControlEvent(...)` construction, the `_publish()` closure and the success log are all
reported missing. Two suites *appear* to cover it and each covers the other half:
`test_promotion_review.py` stubs the activity.

The `extra["subject"]` that **is** the targeting is unverified end to end.

✅ **FIXED 2026-08-22** — `services/medallion/tests/test_request_approval_targeting.py` executes the
activity for real across all four of its paths, with the Dapr client and `publish_event` patched on
their DEFINING modules (both are imported inside the function body, so patching `medallion.workflow`
would bind nothing and the test would pass while the real client ran).

It asserts the two fields that ARE the targeting, and both are mutation-proven rather than merely
green — the point being that every way of getting them wrong yields a healthy-looking event that
reaches nobody:

| mutation | caught |
| --- | --- |
| `"subject": spec.approver` (drop the `user:` prefix) | ✅ |
| `object_id=f"table:{spec.to_dataset}"` (unqualified) | ✅ |
| `return True` from the failed-publish branch | ✅ |

The third is the compensating control the orchestrator's own comment names: *"ASK BEFORE WAITING, and
treat an unsendable ask as a refusal: parking on an event nobody was told about is an outage wearing a
pause."* Returning True there would park the workflow on `promotion_decision` for `approval_hours` for
an ask that never left the process.

**A self-correction worth recording:** my first attempt at that third mutation did not apply — the
pattern did not match — and the suite stayed green. A mutation that silently fails to apply is
indistinguishable from a test that does not bite, which is the exact defect class this audit is about.
Re-applied with an `assert old in s` guard, it fails as it should.

**Additionally ENFORCED** — `tests/unit/test_control_action_three_file_contract.py`, from the skill's
"a new named action is a THREE-file change". Measuring the three directions corrected a claim I had
written into its own docstring:

* A missing `NotificationReason` is **already fail-fast, and not via `as_delivery`**: `inbox.py:46`
  builds `_CONTROL_REASONS` from `NAMED_ACTIONS` at MODULE scope, so the service refuses to import.
  Verified by mutation — collection dies `ValueError: '...' is not a valid NotificationReason`, exit 2.
  The skill's 2026-08-16 incident is a ROLLBACK hazard (old code, new data) that no gate here can
  catch; forward, the import guard holds. My assertion is kept for the named message, not the coverage,
  and now says so.
* A missing `NAMED_ACTIONS` entry fails **SILENTLY** and nothing else catches it — IGNORED with a
  SUCCESS ack, no retry, no error log, producer tests green. RED proof: adding `table_shared_with_user`
  to `ControlAction` alone fires it by name.

`ControlAction` is a superset (36 actions, 8 targeted): the other 28 are object-lifecycle events, listed
in `_UNTARGETED_ACTIONS` rather than matched by a `table_*` prefix rule — a prefix would wave through a
future `table_shared_with_user`, and matching a name shape instead of the fact is the defect class this
audit keeps finding. 32 passed.

### M5 — the media DROP path's FAIL emit never runs, inside a `suppress` · **CONFIRMED**

`transform.py:666-704`. The `UnderivableMediaError` branch returns `_DROP`, so Dapr will **not**
redeliver — as the code's own comment says, "a lost FAIL publish means the failed run is NEVER recorded
and NEVER retried: the graph silently forgets it." The FAIL emit is the compensating control. It is
executed by no test **and** it is wrapped in `with suppress(Exception)` at `:676`, so a defect inside it
produces silence rather than a red.

✅ **FIXED 2026-08-22** — `services/medallion/tests/test_media_drop_fail_emit.py` drives the branch and
asserts three things, each mutation-proven:

| assertion | RED mutation | caught |
| --- | --- | --- |
| a deterministic media failure DROPs | — (pinned as the contract) | — |
| exactly one FAIL run is recorded | delete the FAIL emit block | ✅ got 0 |
| the FAIL run carries `lance.originator` | (trap 2 on the failure path) | ✅ |
| a broken emit is LOGGED, not swallowed | `_best_effort` back to `pass` | ✅ |

**The `suppress` is design, not defect, and the fix keeps it.** All four suppressed sites in
`transform.py` are best-effort by intent, and the reasons are written at each: *"a graph outage must not
convert a correct refusal into a retry storm"*, and a FAIL record that cannot be written must not stop
the DROP that keeps a deterministic failure from re-reading every blob from S3 `maxDeliver` times. What
was wrong is that `with suppress(Exception)` threw the DIAGNOSIS away with the exception — and these
blocks ARE the compensating control, so a bug inside one produced exactly the silence the control exists
to prevent. All four now run under a `_best_effort` context manager that logs and still never re-raises.
Applied to all four rather than the one M5 names, because the other three are the same shape and a
half-applied rule is the thing this estate calls sloppy.

A note on driving it: the error is raised from `read_upstream` rather than `transform_stage`. The branch
is keyed on the exception TYPE, not on where in the stage it arose, and the read is the first thing
inside the `try` — so the handler is reached without needing a real upstream dataset on disk. The first
attempt stubbed `read_upstream` to return a bare object and got `RETRY`, which is the honest answer: a
stub that breaks something else does not exercise the branch you meant.

275 medallion tests pass; ruff + `ty` clean.

---

## Part 5 — Gates that report green while measuring less than they claim

v1 found two of these (nav-truth's bounded-window regex; transport-contract's `slice(0, 600)`) and
closed both. This is the rest of the class.

### H13 — the LANCE-ONLY invariant's call site is unwired · **CONFIRMED, HIGH**

`services/catalog/src/catalog/api/v1/endpoints/data.py:170`. CLAUDE.md calls LANCE-ONLY "a permanent
ruling, not a current-scope note," enforced by `_reject_unsupported_format`. **Replacing that single
call with `pass` leaves the offline suite at `1 failed, 4,849 passed` — and the one failure is the
pre-existing ordering flake, not the guard.**

`tests/unit/test_format_guard.py` calls the guard *function* directly. Nothing tests that the endpoint
calls it. And the bypass half is worse: **`declare_table` and `register_table` accept the same
`properties` and never call it at all.**


**ENFORCED + FIXED 2026-08-22 — and the bypass was TWICE as wide as filed.** `tests/unit/test_format_guard.py::test_every_door_that_accepts_properties_calls_the_format_guard` derives the door list from the spec request MODELS rather than listing it, so a new door that accepts `properties` fails on the day it lands. It found **four** unguarded doors, not the two filed: `declare_table`, `register_table`, **`create_namespace`** and **`update_table`** — all four take a `properties` map through models that genuinely carry the field (verified: 17 spec models do, no substring false positives). The guard moved from a module-private helper in `data.py` to `catalog/core/formats.py::reject_unsupported_format` and all four now call it. RED/GREEN: deleting the call from any single door fails the gate naming that door. **My first version of the gate was itself incomplete** — it scanned body-model annotations only, so `create_table`, the door the guard was written for, was invisible (it takes `properties` as a spec-0.9 query param). Found by deleting that call and watching the gate stay green; the gate now covers both shapes. 3,127 passed, ruff + `ty` clean.
### H14 — the authn-audit compliance gate is a file-wide substring count · **CONFIRMED, HIGH**

`tests/unit/test_invariants.py:729` is `assert src.count("audit(") >= 2` over the whole of
`security.py`, which currently contains **10** audit calls. Its docstring claims the gate proves
`authenticate` audits both the success and the failure paths. **Eight of the ten — including the
SUCCESS audit on the service-credential door — can be deleted and the gate still passes.**


**ENFORCED 2026-08-22.** `tests/unit/test_invariants.py::test_authentication_outcomes_are_audited`. `assert src.count("audit(") >= 2` over a file holding **ten** audit calls is replaced by a STRUCTURAL check: every `raise`/`return` in `authenticate` must have an unconditional `audit(` earlier in its own block, with exempt outcomes named in `_UNAUDITED_AUTHN_OUTCOMES` rather than absorbed by a floor. Deletable audits: **8 → 0**. RED/GREEN: deleting the service-door SUCCESS audit, the `public_caller` FAILURE audit, or the principal SUCCESS audit each fails the gate; all three passed the old floor. Deliberately NOT a proximity window — nine audits sit one line above their outcome and the tenth is separated by an eleven-line comment, so any window would be tuned to that comment and rot when it was edited (the nav-truth / transport-contract failure). **My first version was too permissive and the RED run caught it:** `audits_in(stmt)` walked whole compound statements, so an `if` that audited and then RAISED marked every later outcome as covered even though that audit never runs on the path reaching them — deleting the service-door audit still passed. Only an unconditional straight-line `audit(...)` now marks a block covered.
### H15 — the BFF-caller gate is satisfied by a doc comment · **CONFIRMED, HIGH**

`frontend/packages/zone-contract/src/bff-routes.test.ts:99`. The gate that proves every BFF proxy route
has a caller does `sources().filter(f => f.text.includes(pattern))` across the **whole** frontend tree.
So a **doc comment mentioning the path**, another zone's file, or an e2e mock that fakes the route all
count as callers. Proved under mutation.


**ENFORCED 2026-08-22.** `frontend/packages/zone-contract/src/bff-routes.test.ts`. `sources()` now blanks COMMENT bodies before matching, so a route named in prose no longer counts as its own caller. Deliberately a quote/template/comment state machine, not a regex: `'https://viewer.example/api/annotations'` contains `//`, and a naive line-comment strip would truncate a string literal and could DELETE a real call site — turning a false pass into a false FAILURE. Comment bodies become spaces so offsets still line up. RED/GREEN is asserted on the MECHANISM (6 new `stripComments` tests) rather than end-to-end, and the reason is stated in the test file: proving it through the gate needs a route whose only estate-wide mention is a comment, which is not a state this estate happens to be in — so an end-to-end mutation would prove nothing about the rule that was wrong. Making `stripComments` a no-op fails 2 of the 6. 1,249 tests pass across zone-contract's 22 files; oxlint and oxfmt clean. **Not closed by this:** the audit's sibling claim that an e2e mock or another zone's file can satisfy the caller check still stands — the scan root is unchanged.
### H16 — the estate-navbar zone roster is a hand-written literal · **CONFIRMED, MEDIUM**

R15 ("a zone missing from the shared navbar is a defect regardless of scaffold status") is guarded only
by a **hand-written six-title array** inside `@rask/ui`'s own test (`nav-config.test.ts:32`). No gate
anywhere compares the estate navbar against the zone directories — yet the test's own comment claims
"a zone scaffolded without an entry fails here." It would not.


**ENFORCED 2026-08-22.** `frontend/packages/zone-contract/src/nav-truth.test.ts` gains *"R15: every zone has a SHELL navbar entry"* — the direction nothing checked. `nav-truth` already proved every navbar href resolves to a real route (nav → routes); this proves every zone is reachable from the navbar at all (zone → nav). The roster is DERIVED via `zoneDirs()` (a directory carrying a `package.json`, the same rule bun's workspace glob uses), so a scaffolded zone changes the gate's input. RED/GREEN: creating `frontend/microfrontends/auditzone/` fails with *"the auditzone zone ships but the estate navbar links to nothing under /auditzone"*, while `@rask/ui`'s hand-written array passed all 23 of its tests on the same tree — which is the finding, demonstrated. Filesystem, never `git ls-files`: H5 established the CI container has no `git` binary and `.dagger/frontend.go` strips `.git`, so a git-derived roster would be a gate that cannot run where it matters. 1,256 tests across 22 files; oxlint + oxfmt clean. The `@rask/ui` literal is left in place — it asserts ORDER and tier, which the derived gate deliberately does not.
### M6 — `.dagger/charts.go` renders a config the chart refuses, and the pytest renders can't see it · **CONFIRMED**

`ms-charts` fails with:

```
Error: execution error at (rask/templates/services.yaml:41:21): image.repository must be set to a registry
```

`Charts()` runs bare `helm template chart`. `image.repository` became `required`
(`_helpers.tpl:833`) in **`3c909e0a` on 2026-08-04 — 923 commits ago** — and `charts.go` was never
updated. That is the CI failure.

The audit finding is the *other* half: `tests/unit/test_invariants.py:401`'s `_helm_template()` always
appends `--set image.localImages=true`. So **all 13 chart-render invariants render the side-load path
and none renders the production registry path.** Two artifacts render the same chart; only one
exercises the branch production uses, and it is the one that has been red for 923 commits.


**FIXED 2026-08-22** (commit `028cb717`). `.dagger/charts.go` now renders the PRODUCTION registry path via a shared `renderArgs`, deliberately *not* `image.localImages=true`: the thirteen pytest invariants all pin the side-load path, so this gate is where the registry path gets covered and the pytest suite keeps the other. Both paths are now rendered by something.
### M7 — `MIN_SUITE_FILES` slack grew from one suite to two · **CONFIRMED, LOW→MEDIUM**

`tests/unit/test_e2e_collection_gate.py:54` pins `MIN_SUITE_FILES = 24` against **26** suite files that
collect today. Two suites — and the challenger notes the registry-CAS *and* governed-union suites are
both candidates — can stop collecting entirely while all four assertions in the gate report green. That
is the exact failure the constant's own comment says must not exist, at a scale of two.


**ENFORCED 2026-08-22.** The hand-maintained `MIN_SUITE_FILES = 24` is gone. Collection must now REPRODUCE the set of `test_*.py` files on disk exactly — no slack, nothing to maintain, and it names the specific suite that stopped collecting instead of reporting a number. The floor had drifted before (its own comment records 22 → 24), so the slack was structural rather than a one-off. Same self-consistency fix nav-truth used when its `> 30` sat under a scanner seeing 80 of 90 hrefs. RED: making one suite fail to import fails the gate; the old floor tolerated losing two.
### M8 — the marker gate is satisfied by a comment · **CONFIRMED**

`tests/unit/test_e2e_collection_gate.py:169` greps the **raw text** of `.dagger/*.go`, `scripts/*.sh`,
`Makefile` and `.github/workflows/*.yml` for `-m <marker>` — comments included. Deleting a real
`make e2e-<suite>` target and leaving a `# TODO: pytest -m media` comment behind satisfies it. The
`media` marker's site today *is* prose.


**ENFORCED 2026-08-22.** The invocation-site scan reads CODE, not prose: `_without_comments()` blanks `//` and `/* */` for Go and `#` for Make/shell/YAML before the `-m <marker>` regex. Proved directly — a marker named only in a Go comment or a Makefile `# TODO` no longer matches, while a real recipe line still does. This mattered concretely: the `media` marker's only match was the prose `pytest -m media` inside the block comment above `E2E_SUITES`. Over-blanking is possible (a `#` inside a shell string) and is the SAFE direction — it can only report a marker as unselected, a false alarm someone reads, never a false pass nobody sees.
### M9 — the runner-invocation gate passes on a leg that selects zero tests · **CONFIRMED, LOW**

`tests/unit/test_runner_suites_are_invoked.py:85-95` — written to keep v1's M1 fixed — asserts only
that a Makefile recipe line mentions `runners/<r>` and `pytest` and starts with `cd runners/<r>`.
`wired` is a substring match and the `cd` check is `startswith`, so nothing constrains what follows
`pytest`: a leg narrowed to select nothing passes. It is also **silent about the seven of nine sealed
runners that ship no tests at all**, because it enumerates only runners that already have a `tests/`
directory.

✅ **FIXED + ENFORCED 2026-08-22** — both halves, and they needed different fixes.

*Being named is not being run.* The two original assertions are satisfied by a leg narrowed until it
selects nothing, because `wired` is a substring match and the `cd` check is a `startswith` — everything
after `pytest` was unconstrained. The gate now rejects the narrowing surface directly: no `-k`,
`--deselect`, `--ignore`, `--lf`/`--stepwise`, and the only sanctioned `-m` expression is `not slow`
(the one deselection that separates `test` from `test-slow`). Plus a floor that an invoked suite is not
empty. That floor is **stated as a floor**: it reads the files rather than executing pytest, because
running the suite for real needs the runner's sealed venv and the root suite must not depend on one
existing — that dependency is the reason these runners are sealed at all.

*The silence.* Every assertion in the file enumerates `_sealed_runners_with_tests()`, so a runner with no
`tests/` is invisible to all of them — **seven of nine are**, and the gate reads exactly as green as it
would if all nine were covered. Same shape as H3's `--continue`: a report that stops early is
indistinguishable from a clean one. The roster of suite-less runners is now frozen in
`_RUNNERS_WITHOUT_TESTS`. This deliberately does **not** demand tests from a sealed runner — that is the
workload's call, and `runners/*` is sealed so those calls stay local — it demands only that the set be
stated, so a tenth runner arriving with no suite fails here and someone decides on purpose.

Three RED proofs, each restored and verified clean afterwards: `-k no_such_test` on the dummy leg fires
the narrowing assertion; `-m "not slow and not integration"` on the htr leg fires the mark assertion; a
tenth runner with a `pyproject.toml` and no `tests/` fires the roster naming `zzprobe`. 9 passed.

### Smaller gate-reach findings (all confirmed, LOW)

- **`view-transition.test.ts:28`** — the gate whose docstring says "this is the gate that keeps
  [the cross-document at-rule] out" reads **one stylesheet of eleven**, and none of the seven zone
  `app.css` files a person chasing the cross-zone flash would edit. Repo-wide grep finds no second gate.
  **ENFORCED 2026-08-22:** now scans every AUTHORED stylesheet (11 today) instead of one, with a non-vacuity floor so a moved walk root fails loudly. Build output (`storybook-static/`, `dist/`) is excluded at the walk rather than filtered after — a compiled copy would report a violation no source contains, and the gate's answer would depend on whether someone had run a build. RED: adding `@view-transition` to `studio/src/app.css` now fails naming that file; the one-file version could never see it. 15 passed.
- **`no-networkidle.test.ts:41`** — scans only `microfrontends/<zone>/e2e`, so the estate's surviving
  `waitUntil: 'networkidle'` calls sit outside it. Three of the seven zones (`compute`, `studio`,
  `models`) have no `e2e/` at HEAD, so the gate is vacuous for them too — up from two in v1.
- **`single-flight-keys.test.ts:121`** — the refresh-site regex `\{[^)]*\}` cannot match a key literal
  containing a call, so the gate judges 15 of the estate's 40 `.refresh()` sites while its own
  anti-vacuity guard passes.
- **`test_oidc_discovery_parity.py:41`** — the `OIDCVerifier\((.*?)\n\s*\)` regex anchors on a newline
  before the closing paren, so a single-line construction is invisible to the scan; the `>= 5` floor
  against 8 found doors then absorbs three disappearances.
- **`test_ray_job_images.py:33`** — gates `.docker/ray-lance.dockerfile`, a demo image the chart does
  not deploy, while KubeRay runs `.docker/ray-cluster.dockerfile`. *(Filed, then partly refuted — see
  Part 9; the mechanism is real but the fix is not deletion.)*

---

## Part 6 — Suites that exist and run nowhere

### H17 — 12 of 26 live e2e suites are in a CI lane; the other 14 are in none · **CONFIRMED, HIGH**

Mapped exhaustively: the CI-reachable `tests/e2e-py` suites are precisely the 12 files named as **paths**
by `scripts/e2e_stack.sh:277-284,304-305` (8), `scripts/ray_e2e_stack.sh:158-161` (3) and
`.dagger/e2e.go:38` (1). **No `run:` line in `ci.yml` names any of the 13 per-suite markers.** That
leaves **46 of the 88 live assertions** — including the medallion cascade proof, the governed-union
authz proof, the registry-CAS proof and the GPU-free dummy lane — running in no automated lane at all.


**MIGRATED 2026-08-22.** Mechanism → `.claude/skills/rask-architecture` § Hard invariants (the globbed-membership vs explicit-`testpaths` asymmetry). Work item → `open_python-audit.md` E9, with the 12-of-26 map and the exit-0-on-all-skip mechanism. Nothing is lost when this file goes.
### H18 — v1's H1 was closed cosmetically, and both ends of the gate fail open · **CONFIRMED, HIGH**

v1's H1 produced 13 `make e2e-<suite>` targets (`Makefile:709-734`). They exist and each selects a real
marker. But:

1. **No CI job invokes any of them** — verified by grep; the Makefile's own comment at `:690-692`
   admits it ("wiring the security-shaped ones … into CI is the follow-up").
2. **`pytest` exits 0 when every selected test skips**, so each target reports success while executing
   zero assertions against an undeployed stack.
3. **The gate written to keep H1 closed accepts a bare Makefile target as an "invocation site"** (M8).

So H1's substance — the security-shaped suites run in no lane — is **unchanged**. Only its symptom was
removed, and the removal satisfied the gate that was supposed to prevent exactly this.


**PARTIALLY ENFORCED 2026-08-22, remainder MIGRATED.** The gate half is closed — M7 removed the floor's slack and M8 stopped a comment counting as an invocation site, so a lost suite is now loud at both ends. The other half is NOT a gate defect and is not fixed here: 14 suites still run in no CI lane, and `pytest` exits 0 when every selected test skips. That is H17, already MIGRATED to `open_python-audit.md` E9 as a scope decision — wiring live lanes is infrastructure work, not a test edit, and pretending otherwise would be the 'workaround that makes a gate pass while measuring nothing' this audit is about.
### H19 — the ingest DLQ/poison-park suite skips in every CI lane · **CONFIRMED, HIGH**

`services/ingest/tests/test_worker_queue.py:44` is a module-level
`pytestmark = pytest.mark.skipif(not _reachable(), reason=f"no NATS at {NATS_URL}")` over a TCP probe.
`.dagger/test.go`'s `Test()` **binds no NATS service, passes no `-rs`, and has no all-skip guard.** So
the only tests of "the stream IS the ledger", the exactly-once commit chain and the DLQ poison-park
vanish in CI while the job prints green — dropping `ingest/worker.py` from **82 % to 35 %** coverage on
a run that reports success.

**This is v1's M6.** M6 was closed with a real fix (`ensure_dlq_stream`, still at HEAD in
`queue.py:170`). The closure is genuine in the source and **fiction in the gate**: the regression test
that proves it runs only on a developer box with a `kubectl port-forward` holding `127.0.0.1:4222`.
Two of the seven skipped tests need no broker at all.

### H20 — seven of nine sealed runners have no tests, and the two that do run in no CI job · **CONFIRMED, HIGH**

`.dagger/test.go:139` is the single pytest exec of `Test()`; it runs the root `testpaths`, which list
no runner path. Measured: that selection collects **4,857 tests, of which zero come from `runners/`**.
No `ci.yml` step, no e2e script and no Dagger function ever `cd`s into a runner.

- The 75 test functions that exist (56 `htr` + 19 `dummy`) execute in **no CI job** — v1's M2, unchanged.
- **Seven runners** (`asr`, `assist`, `diarize`, `insid3`, `kg`, `topics`, `voiceprint` — 3,623 tracked
  Python LOC) have **no tests at all**, no lockfile in six cases, and are excluded from `ty`. The
  challenger corrected the finding upward: in CI the number of checks reaching them is **zero**, not
  one — `ruff` is local-only.
- M9 explains why the estate's own runner gate cannot notice.


**MIGRATED 2026-08-22, with one sub-claim REFUTED.** Mechanism → `.claude/skills/rask-architecture` § Hard invariants; work item → `open_python-audit.md` E9. **Refuted:** "no lockfile in six cases" is not a defect. That skill's plane table states a runner carries a `uv.lock` *only where it builds an image* — `assist`, `dummy`, `htr` — and the offline Ray Data runners let Ray install the env via `runtime_env`. My own measurement matches that exactly, so the absence is the documented design. Reading the skill in full is what caught it; the surviving half (seven runners with no tests, 75 test functions in no CI job) stands.
### H21 — 14,504 lines of Python have zero tests and no `testpaths` entry · **CONFIRMED, HIGH — and this audit understated it 5.5×**

As filed, this named `services/search` alone. Cross-checking against `open_python-audit.md`'s E9 (which
found the same class two weeks earlier) and re-measuring:

| | in `testpaths` | test files | tracked `.py` LOC |
| --- | --- | ---: | ---: |
| `services/search` | **no** | **0** | 2,614 |
| `services/viewer` | **no** | **0** | 4,288 |
| `packages/ratch` | **no** | **0** | 7,602 |
| | | | **14,504** |

`packages/ratch` is the largest untested unit in the estate — the media-pipeline library, and the one
package sanctioned to ship a console script. This audit's twelve lenses found `services/search` and
missed the other two; E9 found `ratch` and `viewer-search` in 2026-08 and they are still open 490
commits later.

CLAUDE.md says rask "lets you ANNOTATE and SEARCH the data"; half of that sentence has no test
directory. `services/search/.../frames.py:41`'s `_ranked_or_fallback` runs at **16–33 % line coverage**
and its shape is `try: return rank(scoped=True) / except: pass` → fall through. **A search plane that
has stopped ranking anything returns an empty 200 that no test can tell apart from "no hits."**
Survived every refutation axis including mutation.

*E9's other half DID land:* `services/catalog/tests` and `services/lineage/tests` — the two suites that
pinned a privilege-escalation and a commit-duplication regression while collected by nothing — are both
in `testpaths` now. Enrolling the three above buys nothing on its own, because there is nothing to
enroll. That makes this an epic, not a two-line fix.


**MIGRATED 2026-08-22.** Mechanism → `.claude/skills/rask-architecture` § Hard invariants, including that the enrolment gate covers `packages/*/tests` and `services/*/tests` but NOT a new top-level `tests/<x>/` — how `tests/e2e-py` was lost once. Work item → `open_python-audit.md` E9 with the per-unit LOC table.
### H21b — the authorization subtree is unlinted in CI and carries a 21-rule exemption when it is linted · **HIGH**

Two findings that are only serious together, one from each audit.

`pyproject.toml:126` grants `packages/service-kit/src/service_kit/governed/**` a blanket exemption from
**21 ruff rules** — including `S110` (try-except-pass), `S112` (try-except-continue), `S324` (insecure
hash), `S608` (SQL injection) and `C901` (complexity). `open_python-audit.md` flagged the exemption's
existence; what it could not know is **M25**: `.dagger/checks.go:17-18` lints only `services` and
`tests`, so `packages/` is not lint-checked on the merge path **at all**.

So the estate's authorization kernel is: outside the CI lint gate, and — on the developer path where it
*is* linted — exempt from four security rules and the complexity ceiling. Neither half is indefensible
alone. Together they mean no automated check reads that subtree for the rule classes it most needs.


**FIXED 2026-08-22.** Both halves. The CI-scope half is closed by M25 above. The exemption half: `packages/service-kit/src/service_kit/governed/**` went from a 21-rule blanket to the **four rules that actually fire** (`ANN401`, `ANN202`, `C901`, `PERF401`), so the four SECURITY rules it was suppressing on the authorization kernel — `S110`, `S112`, `S324`, `S608` — are live again. RED proof: appending a `try/except/pass` to `governed/fga.py` now fires `S110`; under the old row it did not. The `schemas/**` row was DELETED rather than narrowed — not one of its 21 rules fired, so it suppressed nothing while implying the subtree needed suppressing. Narrowing surfaced four dead `noqa` directives (`RUF100`) the blanket had hidden; removed, keeping their rationale comments. **Scope bound, stated:** the identical 21-rule list appears on 22 other rows. This pass narrows two. `C901` survives on one function (`fga.py::expand_tree`, complexity 17 vs a ceiling of 15) — splitting the authz kernel's tree expansion is a separate commit, not a silent tack-on.
### M10 — the live auth suite is selected by nothing, and the job named `e2e-auth` does not run it · **CONFIRMED**

`tests/e2e-py/test_auth_e2e.py:25` carries only the generic `e2e` marker — **no per-suite marker, no
path-invocation site**. No make target, script, Dagger function or CI job selects it. The CI job
literally called `e2e-auth` runs `scripts/auth_e2e.sh`, a different artifact.

And v1's H2 fix, which rewrote this file's assertions, **introduced a new defect while removing the old
one**: `child = 'e2ens.e2child'` at `:109-118` uses `.` where the catalog's namespace delimiter is `$`
(`catalog/core/config.py:29` default `$`; `.docker/docker-compose.yml:53` `LANCE_NS_DELIMITER: "$$"`).
So the request it expects to be a 403 *nested* create is a **200 root create**, and the
`_grant(..., 'namespace:e2ens')` that follows is inert again — which is precisely the defect the fix's
own comment says it removed.

✅ **FIXED + ENFORCED 2026-08-22** — both halves, and the first one measured worse than filed.

*Selected by nothing.* Verified by counting invocation sites across the Makefile, CI, `.dagger/*.go` and
`scripts/*.sh`: `test_auth_e2e.py` had **0**, and it is the estate's ONLY live suite with none — the
other nine that carry the generic `e2e` marker alone are all invoked BY PATH, so they were never
orphaned. The audit's framing (a missing per-suite marker) was therefore the symptom; the defect is that
neither selection mechanism named this file. Worse than filed: `scripts/auth_e2e.sh`, the artifact the
CI job named `e2e-auth` runs instead, contains **no pytest invocation at all**. The estate's live
authorization proof was selected by nothing, under a job whose name asserted the opposite. Fixed with an
`auth` marker, its `pyproject.toml` registration, and an `e2e-auth` make target following the fifteen
existing per-suite targets exactly; `pytest -m auth` now collects it (1 of 88, and the module holds one
test).

*The `.` where the delimiter is `$`.* Confirmed against `catalog/core/config.py:29` (default `$`) and
`.docker/docker-compose.yml` (`LANCE_NS_DELIMITER: "$$"`, compose-escaped). `"e2ens.e2child"` is not a
child of `e2ens` — it is a single ROOT-level namespace whose name contains a dot. So the create gated on
the root, the 403 could pass for an unrelated reason (a locked root), and the `_grant` on
`namespace:e2ens` that follows was inert against it: exactly the defect v1's H2 fix says it removed,
reintroduced in a different disguise by the fix that removed it. Now `f"e2ens{DELIMITER}e2child"`, with
the delimiter read from the environment so the suite follows a stack configured differently rather than
asserting against one only this file believes in.

Enforced by `test_every_live_suite_is_selected_by_something` — DERIVED, not a roster: a suite must have
either a per-suite marker (which the existing `test_every_declared_marker_has_an_invocation_site` then
requires a make target for) or a path invocation. It reads the selection surfaces with comments
STRIPPED, because a suite named only in a commented-out recipe is selected by nothing and a raw
substring search would call it covered — the same M8 class as the chart gates. RED: removing the
`e2e-auth` target fires it naming `test_auth_e2e.py`.

A second gate came out of adding the target: `E2E_SUITES` exists only to build the `.PHONY` list, and
nothing tied it to the recipes — so the sixteenth suite needed an edit in two places and I missed the
second on the first pass. `test_every_e2e_target_is_declared_phony` now derives both sets from the
Makefile and compares them. 7 passed; full unit suite 2858 passed, 1 skipped, exit 0.

### M11 — the standalone browser suite skips every test on any default deploy · **CONFIRMED, HIGH→MEDIUM**

v1's M3 correctly stopped `tests/e2e/tests/mfe.spec.ts` from counting an OIDC bounce as a pass. It did
so with `test.skip`. **The chart has shipped `auth.enabled: true` by default since 2026-08-06**, so all
seven route tests now skip against any default estate and `make e2e` exits 0 having exercised no zone.
The escape hatch the skip message offers is unimplemented: `tests/e2e/playwright.config.ts` has no
`globalSetup` and no `storageState`.

✅ **FIXED 2026-08-22.** Both halves, and the second one is why the first mattered.

*The run, not the route.* Each route test skipping on an auth bounce is CORRECT and stays — that is
v1's M3 fix, and an untested surface must not be indistinguishable from a passing one. What that fix
could not do is speak for the RUN: seven honest skips still add up to a dishonest run, and `make e2e`
exits 0 having exercised no zone. A single precondition test now asserts the target is reachable and
not auth-gated, and **fails** rather than skipping. Skipping is the right answer to "this route was not
covered"; it is the wrong answer to "nothing was covered and nobody will be told".

*The hatch the message offered did not exist.* The skip text tells a reader to "give this suite a
signed-in storageState" — with no `storageState` and no `globalSetup` in the config, so the advice was
unfollowable and the suite had no way to run against the estate the chart actually ships. The config
now honours `RASK_E2E_STORAGE_STATE`, and the failure message names it. Advice a gate gives has to be
executable, or the gate is telling people to do something impossible and then passing anyway.

**Also closes the `no-networkidle.test.ts:41` gate-reach bullet in *Smaller gate-reach findings*.** That
gate scanned only `microfrontends/<zone>/e2e`, and three of seven zones ship no `e2e/` at all, so it was
vacuous for them AND blind to the estate's two other browser-driving trees. Widened to `tests/e2e` and
`@rask/ui/harness`, it immediately caught two survivors — including `mfe.spec.ts:47`, on the very
navigation whose result it then asserts. A second fix was needed to catch the second one: the file
matcher required `.spec`/`.test`/`.e2e` in the name, which a zone's `e2e/` needs (ordinary source sits
beside its specs) but a DEDICATED browser tree does not — every script in one drives a browser, which is
how `harness/drive.mjs` kept its wait. Both now use `domcontentloaded`. 9 passed.

Compounding it: the same file still calls `page.goto(route, { waitUntil: 'networkidle' })` at `:47` —
the wait the estate has ruled can never fire on a page holding a live stream — and the gate that bans
`networkidle` does not scan `tests/e2e` (Part 5).

### M12 — five notification-lane drives are referenced by nothing · **CONFIRMED**

`tests/e2e/verify_originator_lane.mjs`, `verify_task_assignment_lane.mjs`,
`verify_task_departure_lanes.mjs`, `verify_lease_lapse_lane.mjs`, `verify_notifications_two_users.mjs`
are tracked, documented, and referenced by **zero** files in the repository (`grep -rl` each: one hit,
its own declaration). They also sit outside `testDir: './tests'`, so even `make e2e` skips them.

Read what one of them says it proves:

> "The claim under test is the one no unit test can make: that a run whose AUTHOR is not you still
> reaches you, on a real Dapr actor plane, because you are named as the human the work is for."

✅ **FIXED + ENFORCED 2026-08-22** — re-verified first: `grep -rl` still returns exactly TWO hits per
drive today, and the second is this audit file naming them. Nothing else in the repository referenced
any of the five.

`make notifications-lanes ORIGIN=…` names all five and refuses without an `ORIGIN` rather than silently
driving localhost. A named target rather than an offline gate because these need a DEPLOYED estate — a
live Dapr actor plane is precisely what makes them prove something a unit test cannot.

Enforced by `test_every_notification_lane_drive_is_invoked_by_something`, the same rule as the live-suite
gate above applied to the other kind of live proof, and comment-stripped for the same reason (a drive
named only in a commented-out recipe is invoked by nothing). RED: deleting the target fires it naming
all five; GREEN: 8 passed.

**A separate defect found while fixing this one, and it is not M12's:** the adjacent
`notifications-rig-up` / `-down` targets run **`docker compose`**, which the estate's hardest rule
forbids outright — "no docker command, for any purpose, including throwaway containers". A survey found
**18 docker invocations** across `Makefile` and `scripts/`. That is a scope decision spanning legitimate
bootstrap exceptions (`dagger-engine.sh` cannot use Dagger to create the Dagger engine) and plain
violations, so it is **MIGRATED**, not patched here — see N1 below.

The ORIGINATOR lane — the targeting source that is *structurally unreachable* from a unit test, and
whose producer-side field M3 shows is unguarded — is proven only when a human remembers to type
`node tests/e2e/verify_originator_lane.mjs`.

### M13 — `make e2e` is in no CI job · **CONFIRMED**

`Makefile:769-770`. Grep for `make e2e` not followed by `-` across `.github/workflows/`: no match.

**MIGRATED 2026-08-22.** Same class as H17 and migrated beside it — `open_python-audit.md` § E9, under
the addendum that now names BOTH layers and the difference between them (H17 is `tests/e2e-py`'s 14
unreachable suites; this is the standalone Playwright project, in no job at all).

**The state changed while this audit ran, and that is why it is a decision rather than a wiring
change.** Adding `make e2e` to a CI job before 2026-08-22 would have bought a lane that passed having
tested nothing: the chart ships `auth.enabled: true`, every route test skipped on the OIDC bounce, and
the run exited 0. M11's fix makes that case FAIL and gives the config a real `RASK_E2E_STORAGE_STATE`
hook. So the open question is now sharp and stated: **what identity does CI drive the browser suite
as** — an auth-off install, or something that produces a signed-in storage state? Adding the job
without answering it yields a red lane rather than a fake green one, which is an improvement and still
not something to land silently.

Two mechanical notes for whoever does it: `.github/workflows/ci.yml` is held by a concurrent session in
this tree, and `make e2e` needs `bun install` in `tests/e2e` first — it carries its own lockfile.

### M14 — five e2e suites are gated on env vars nothing assigns · **PARTIALLY REFUTED — see Part 9**

The lens filed five; the challenger's control experiment killed the causal claim and found two wrong
line references and a 6× overcount. **The surviving core:** `tests/e2e-py/test_gateway_e2e.py` is a dead
file — nothing in the repo sets `LANCE_E2E_GATEWAY_URL`, so its three tests skip everywhere, and its
assertions at `:41, :48, :51, :57` contradict the current gateway's route table anyway. That is v1's M5,
still open, and now **worse**: the `make e2e-gateway` target its docstring cites now *exists*, so
following the docstring runs three skips and exits 0.

✅ **FIXED 2026-08-22 — and the root cause is one layer below where the finding pointed.**

Re-measured: `make e2e-gateway` collected 3, skipped 3, **exit 0**, exactly as filed. But setting
`LANCE_E2E_GATEWAY_URL` did not help — the fixture skips a SECOND time on `gateway not reachable`. So
the suite could not fail for infrastructure reasons at all: against a dead estate and against a healthy
one it reported the same success. The unset variable was never the whole story; the suite was
**unfailable**.

Three fixes:

1. **An unreachable CONFIGURED target is now a failure, not a skip.** Unset → skip, because an offline
   run should not demand a deployed gateway and nobody asked. Set-but-unreachable → fail, because
   setting the variable IS the request and a request that cannot be served is a failure. Measured:
   unset gives `3 skipped`; `LANCE_E2E_GATEWAY_URL=http://127.0.0.1:1` gives **3 errors**.
2. **The stale routes.** The file asserted `/lineage/livez` and `/catalog/readyz`; the gateway
   registers `("/api/catalog", …)` and `("/api/lineage", …)` at `gateway/__init__.py:144-145` and has
   **no `/api` catch-all**, so both were aimed at routes that 404. This is the reusable part: an
   assertion inside a suite that cannot run is invisible twice over — nothing runs it, and nothing
   fails when the code it describes moves underneath it.
3. **`make e2e-gateway` requires the URL** rather than skipping into a green, the same shape as
   `notifications-lanes`. A live drive with no live target is a failed invocation, not a pass.

### M15 — `home#test:e2e` is red at HEAD independently of everything else · **CONFIRMED, HIGH**

`frontend/microfrontends/home/playwright.config.ts:98-100`. The auth-OFF `chromium` project matches
`/\.spec\.ts$/` and ignores only `e2e/(projects|settings)/`. `e2e/notifications/watch-enrolment.spec.ts`
is therefore collected **a second time**, against the wrong dev server, where all 3 of its tests fail —
while the `chromium-notifications` project at `:135-138` collects the same files correctly.


**FIXED 2026-08-22.** `home/playwright.config.ts` auth-OFF project now matches `/\/e2e\/[^/]+\.spec\.ts$/` — the TOP LEVEL of `e2e/` and nothing below it — instead of `testIgnore: /e2e\/(projects|settings)\//`. Deliberately NOT `|notifications` appended: that hand-list is what drifted, and stating the RULE means the next subdirectory cannot repeat it. Verified by collection: `--project=chromium` now lists 4 tests in `auth.spec.ts` alone, and `--project=chromium-notifications` still lists the 3 `watch-enrolment` tests — so the spec is collected exactly once, by the project whose server can serve it.
### M16 — the lakehouse warmup project matches a directory that does not exist · **CONFIRMED**

`frontend/microfrontends/lakehouse/playwright.config.ts:126` is
`testMatch: /e2e\/(data|lineage|storage)\/warmup\.setup\.ts/`. The zone has **no `e2e/data/`** and does
have `e2e/catalog/`, so `e2e/catalog/warmup.setup.ts` is collected by nothing and the catalog area runs
against the cold Vite cache the config exists to pre-warm.


**FIXED 2026-08-22.** `lakehouse/playwright.config.ts` derives the warmup areas from the tree (`e2e/<area>/warmup.setup.ts`, excluding auth-ON `admin`) instead of the literal `data|lineage|storage` — where `data` had not existed since the area merge and `catalog` was missing. Verified by collection: `--project=warmup` now lists **3** setups (catalog, lineage, storage) where it listed 2, so the heaviest auth-off area is warmed for the first time. Throws if the derived set is empty, so a moved tree fails loudly rather than warming nothing.
### M17 — the Postgres tracker backend is exercised by nothing · **CONFIRMED, LOW**

`packages/tracker/tests/test_postgres.py:179-180` gates six integration tests behind a
`--postgresql-port` CLI option **no Makefile target, script, CI job or Dagger function passes**. The
package's stated contract is backend-agnosticism; its production backend has no runner.

✅ **FIXED + ENFORCED 2026-08-22.** `dagger call tracker-postgres` (`.dagger/tracker.go`), reachable as
`make tracker-postgres`.

| | result |
| --- | --- |
| `uv run pytest packages/tracker/tests/test_postgres.py` | **17 passed, 6 SKIPPED** — the production backend untested |
| `dagger call tracker-postgres` | **23 passed, 0 skipped, exit 0** — all six against a real PG16 |

PG16 because that is what the estate runs (the chart's AGE image is `apache/age:release_PG16_1.5.0`,
CloudNativePG the same major), so this proves the tracker against the version production uses.

**The server is a Dagger SERVICE, never a container the test starts.** The rule admits no exception —
*"Any container — ephemeral brokers, one-off fixtures, ad-hoc debugging — goes through Dagger"* — and a
test fixture is precisely the "one-off fixture" it names. The suite was already shaped for it:
`postgresql_noproc()` connects to a server someone else runs.

Three things had to be got right, and each failed in a way that looked like something else:

1. **`WithDefaultArgs`, not `WithExec`.** The official image refuses to run as root — *"must be started
   under an unprivileged user ID"* — because its entrypoint is what drops privileges. A direct
   `WithExec` of `postgres` bypasses it.
2. **The maintenance DB must differ from the test DB.** With both named `tracker`, pytest-postgresql
   tried to CREATE a database the image had already made: six errors reading `DuplicateDatabase`, on a
   perfectly healthy server.
3. **Trust auth, for a reason that belongs to the SUITE.** `pg_tracker` rebuilds a DSN from
   `postgresql_conn.info`, and `info.password` is empty for a noproc server — so the DSN carries no
   password and scram refuses it. Five tests use the live connection object and passed; only
   `test_postgres_via_factory_end_to_end`, which goes through the DSN, hit it. Trust auth on a
   throwaway server is the right fix; teaching the fixture to carry a password would be changing a
   suite whose job is to test the tracker, not pytest-postgresql.

Enforced by `test_every_opt_in_pytest_option_has_something_that_passes_it`, which scans for
`request.config.getoption("--x")` and requires `--x` to appear in a selection surface. **This is the
THIRD way a suite can be unreachable while reading green**, and the file now gates all three: nothing
SELECTS it (a marker with no target — M10), nothing NAMES it (a drive nobody invokes — M12), or it is
selected and named and then skips itself (here). RED: deleting `--postgresql-port=5432` from the Dagger
call fails **exit 1** naming the file and the option.

---

## Part 7 — Mocks and doubles that stand in for something else

### M18 — the lakehouse's own dev seed describes an API the catalog does not serve · **CONFIRMED**

`frontend/microfrontends/lakehouse/e2e/dev-seed.ts:37-47`. Three of the four catalog bodies describe
routes/shapes the catalog does not serve — `namespaces.py:54` mounts `/v1/namespace` with only
`{id}/…` subpaths and no bare GET — and **two of them provably fail the zone's own valibot schemas**.
Reproduced against the real launcher: `make dev-zone ZONE=lakehouse` renders the warehouses and
storage-tier pages as **502 "catalog contract drift"** — the exact broken state the seed file exists to
prevent.


**FIXED 2026-08-22.** Proven against the zone's own valibot, not by reading: the OLD warehouse seed fails `WarehouseSchema` with exactly three issues — `bucket` missing, `root_uri` missing, `serving` boolean where a string is required — and the new one parses. Also removed two seeds for endpoints that cannot be reached: the catalog's namespace router mounts `/{id}/…` subpaths ONLY (no bare `GET /v1/namespace`), and the mock DERIVES `/v1/stores/tiers` from `STORES` as `{role: Store[]}`, so a flat `{tiers: [...]}` seed shadowed the real shape. Both verified in source before deleting.
### M19 — eleven register-path tests mock a call the estate ruled must never happen · **CONFIRMED**

`services/medallion/tests/test_register_uses_the_service_door.py:32` (and `test_catalog_register.py:56,
65, 79`). Eleven tests still register a respx mock for `POST /v1/namespace/{tier}/create` — a call the
cascade must never make — answering 200/409, statuses the real catalog cannot return for it. **If the
regression came back, 2 of 13 tests would notice and the other 11 would absorb it.**


**FIXED 2026-08-22** (commit `5b5e2a39`). All thirteen dead `POST /v1/namespace/{tier}/create` mocks deleted. `assert_all_mocked` is already on, so the call now raises `AllMockedAssertionError` if it returns — a harder failure than the 200 the mock promised. Nested (`acme$silver`) creates kept: those the cascade does make.
### M19b — every respx test in the estate uses the one form where a dead route is not an error · **HIGH**

*Added after the sweep, from `writing-python/references/testing.md` — the reference states the rule the
twelve lenses had no calibration for: "By default both asserts are on — every routed call must fire, and
every unrouted call raises."*

That is true of the `respx_mock` **fixture** and of the **called** decorator form. It is not true of the
bare one. Read out of respx's own source rather than recalled:

```
MockRouter.__init__ defaults: {'assert_all_called': True, 'assert_all_mocked': True, ...}
respx.mock (the module-level global router)._assert_all_called = False
```

The estate's usage, counted:

| form | count | `assert_all_called` |
| --- | --- | --- |
| `@respx.mock` (bare — the global router) | **118** | **False** |
| `@respx.mock(...)` / `respx.mock(...)` (configured) | **0** | would be True |
| an explicit `assert_all_called=` anywhere | **0** | — |

So **all 118 respx tests in the estate run in the single form that does not check whether its routes
fired.** M19 is not eleven stale routes in one file; it is the general case of a property that is off
estate-wide. A route registered for a call the code no longer makes is silent, everywhere, by
construction — and the reference's own headline rule would have caught it.

**Fix:** `@respx.mock(assert_all_called=True)`, or move to the `respx_mock` fixture, which has it on.
Expect the first run to go red in several places — that redness *is* the finding.

*Clean, by the same reference:* its other hard rule — **never `@patch` an httpx method** — has **zero
violations**. `grep` for `patch(...httpx...)` / `patch.object(httpx...)` / `patch(...AsyncClient.` across
`packages/`, `services/` and `tests/` returns nothing. The estate mocks at the transport layer throughout.


**ENFORCED 2026-08-22** (commit `5b5e2a39`, tracker row 7). `assert_all_called` flipped on the global router in `conftest.py`, covering all 118 bare `@respx.mock` sites and every future one. 17 of 227 went red: 13 dead routes deleted, 6 negative routes now declaring themselves via `respx_allows_unused_routes`.
### M20 — the lakehouse's two session-bearing data-plane routes are mocked away everywhere · **CONFIRMED**

`microfrontends/lakehouse/src/routes/capi/v1/table/[id]/query/+server.ts:16-41` and its `insert`
sibling. Every spec that names their contract intercepts them at `page.route`, and no unit test covers
them. Proved by mutation: replacing **both handler bodies** with `return json({detail:'AUDIT MUTANT'},
{status:500})` — no auth gate, no clamp, no bearer forwarding, no upstream call — left the three specs
**54/54 green**, including one titled "the preview…".

✅ **FIXED 2026-08-22.** Both handlers now have unit tests, and the audit's own mutation is the RED
proof: replacing a handler body with `json({detail:'AUDIT MUTANT'},{status:500})` — the exact edit that
left 54/54 green — now fails **17 tests** on the query route and **9** on insert.

What the specs could not reach, because every one of them intercepts these routes at `page.route`:

* **The auth gate**, and that a refusal is decided HERE — `expect(seen).toEqual([])` asserts an
  anonymous request never leaves the BFF. On the insert route that gate is the only thing between an
  anonymous visitor on an OIDC web tier and a writer-gated data-plane write.
* **The auth-OFF path.** `dev-zone` and every hermetic zone suite run auth-off, so a gate keyed on
  `session` alone rather than `authEnabled && !session` would make the preview unreachable there while
  looking correct in prod.
* **The confused-deputy stance**, which is the security property the route exists for. The test posts
  `{limit, vector, filter, columns, k}` and asserts the catalog receives exactly `{k: 10, vector: {}}` —
  a forwarded `filter` would run under the caller's bearer.
* **The clamp**, parametrized over the cap, the floor, negatives, fractions, and four
  non-numeric/`NaN`/`Infinity` bodies falling back to the default.
* **The two Arrow content-types, which differ by direction** — the read answers `arrow.file`, the write
  sends `arrow.stream`. Swapping them is a silent wire-format mismatch.
* **Upstream status passthrough and the 502.** A writer-gated 403 must reach the UI as a 403; flattening
  it makes a permission problem look like a bug.

Two notes on how they are written. `$env/dynamic/private` is stubbed, which is also what lets the
upstream base be asserted (the handler reads `CATALOG_API` at module scope). And oxlint's
`no-unsafe-optional-chaining` rejected `seen[0]?.init.body` — correctly: the honest fix is not a cast
but an `only(seen)` helper that fails with *"expected exactly one upstream call"*, so a handler that
never called out reports THAT rather than a `TypeError` three lines later. Five lakehouse tasks green
(lint, fmt:check, check, test, build).

So the anonymous-401 confused-deputy gate, the bearer forwarding, the `MAX_PREVIEW_ROWS` clamp and the
`{k, vector:{}}` body the catalog actually receives are asserted by nothing.

### M21 — eleven of fourteen gateway rewrites are pinned to hand-written URLs · **CONFIRMED**

`services/gateway/tests/test_lance_routes.py:53-74`. The `MockTransport` at `:30-34` returns 200 for any
request, so the only upstream check is `str(captured[-1].url) == <literal>`. A rewrite landing on a path
the upstream does not serve passes — and the viewer row's own example, `/api/transcripts`, **is not among
the 33 paths the viewer actually serves.** This is the assertion shape the ingest row already passed
while every `/api/ingest/*` call 404'd in production.

✅ **FIXED + ENFORCED 2026-08-22 — and the fabricated row was real.** Probed the viewer's own app:
**33 OpenAPI paths, and not one of them is `/api/transcripts`** — no transcript route exists at all.
So that row asserted a rewrite that 404s, and passed, because the `MockTransport` answers 200 for any
request and the only check was a string this file made up.

Of the three media rows, exactly one was wrong: `search` serves `/api/search` and `annotator` serves
`/api/annotations/{doc_id}/{speech_id}/{chunk_id}`, both correct. The viewer row is now
`/api/explorer/documents` → `/api/documents`, which the service actually serves.

Enforced by `test_the_media_rewrites_land_on_paths_the_upstreams_ACTUALLY_serve`: it computes the
rewrite with the gateway's OWN route table and `_pick_route`, then checks the result against the
upstream's own OpenAPI paths, template-aware so `/api/annotations/a/b/c` satisfies the `{doc_id}` form.
RED: restoring `transcripts` fails with *"the gateway rewrites /api/explorer/transcripts to
/api/transcripts, which viewer.main does not serve. Its 33 paths do not include it"*.

**The pattern already existed and had simply never been applied here.** `test_routing.py:105` pins the
flows row against the flows app's own openapi and its docstring says why — *"the ingest lesson, applied
to the new row rather than trusted not to recur"*. The lance rows were written the old way beside it.
A lesson learned in one row is not learned until it is applied to the rows that share its shape.

### L1 — two green suites assert opposite things about Ray job metadata · **CONFIRMED, LOW**

`frontend/packages/api/src/ray.test.ts:51-57` asserts `metadata` survives the wire; the backend's own
test asserts the service strips it. Both green. The gate that claims to catch exactly this drift is a
hard-coded literal set that cannot fail.

✅ **FIXED + ENFORCED 2026-08-22 — and the resolution is not "make them agree".**

Both suites are green and only one describes the estate: `RayJob` is `extra="ignore"` and does not
declare `metadata`, so the field the SPA parses is one the service never sends and can only ever be its
`{}` default.

**The Python side is right, for a reason the frontend test could not see.** Stripping is
security-motivated: Ray's `JobDetails` carries `runtime_env` (the job's full env — `S3_SECRET` and the
lineage service token on this estate), and the medallion's own submitter puts **`rask.token`** into the
metadata dict beside `rask.originator` (`ray_submit.py:166`). Retaining it whole would put a token into
every jobs-board row. So the asymmetry is deliberate, and it is now RECORDED as one (`_TS_ONLY`, with
the reason) rather than left to read as drift.

What was false is the SPA test's claim that "the medallion path" delivers it. It cannot: through
`/api/ray/jobs` the field is stripped, and `GET /api/jobs/<id>` — the endpoint `rask-notifications`
names for recovering who a dead job was for — is **Ray's own dashboard API**, not a rask route (the
compute service serves `/jobs` and `/jobs/{id}/logs`, nothing else). Renamed and re-documented.

Enforced by `tests/unit/test_ray_job_wire_parity.py`, which parses BOTH declarations — the Python class
via `ast`, the valibot object literal via its source — so neither can be restated by hand. Three RED
proofs, in three directions: a field added to `RayJob` fires "sends fields the SPA does not parse"
(valibot ignores unknown keys, so it would arrive and reach no surface); a field added to
`RayJobSchema` fires "parses fields the service never sends" (dead shape, and if required it takes the
WHOLE payload down, since the response is parsed as one document); and RESOLVING the asymmetry while
leaving the `_TS_ONLY` note fires "delete the entry", so the explanation cannot outlive the thing it
explains.

**A second defect found while confirming this one:** `test_rays_runtime_env_and_metadata_do_not_ride_along`
names metadata in its title and asserted **nothing** about it — only `runtime_env`. Half the claim in
the name was unchecked, on the model that gets rendered into a lineage event. Assertion added; RED
proof: flipping `RayJobFailure` to `extra="allow"` now fails it. 49 passed.

### L2 — the explorer's mock detects its envelope by key presence · **CONFIRMED, LOW**

`explorer/e2e/mock-media-services.ts:71` uses `'status' in h`, the exact form all three sibling mocks
fixed after it made every seeded route answer HTTP 500 when the payload carried its own string
`status` field.

---


**FIXED 2026-08-22.** `explorer/e2e/mock-media-services.ts` detects its envelope by `typeof h.status === 'number'` instead of `'status' in h`, matching the sibling mocks that already fixed this. Key-presence turned any upstream payload carrying its own `status` FIELD (a health probe, Prometheus' `status: "success"`) into `new Response(body, {status: 'success'})` — a RangeError answered as HTTP 500. `Seeded` already declared `status?: number`, so no type change was needed.
## Part 8 — Test infrastructure

### M22 — the integration client fixture writes to a fixed shared path · **CONFIRMED, raised to MEDIUM**

`tests/integration/conftest.py:64` points the catalog's root at a hard-coded `/tmp/lance-test-root`
instead of `tmp_path`. Real catalog state accumulates across runs, across **concurrent** runs, and
across users on the same host.

✅ **FIXED + ENFORCED 2026-08-22** — THREE sites, not one: the audit named
`tests/integration/conftest.py`, and the same defect sat twice more in `tests/unit/test_user_state.py`
(`_oidc_settings`'s `"root"` and `app_client`'s `LANCE_REST_ROOT`, which have to agree, so both now take
`tmp_path`). 290 passed.

Enforced by `tests/unit/test_no_fixed_tmp_roots.py`, and building it produced the more useful finding.
The first cut flagged every `/tmp/...` literal and caught **eighteen inert ones** — `"/tmp/from"` and
`"/tmp/to"` handed to a MOCKED mover, `"/tmp/a.lance"` as a URI inside an assertion — none of which
touch a filesystem. Exempting them one by one would have built exactly the drifting allowlist the gate
exists to make unnecessary, so the scan now matches **the defect rather than the substring**: the three
forms that point a real service at a root (`setenv("*_ROOT", ...)`, a `{"root": ...}` mapping, a `root=`
keyword). Both original defects are written in those forms; a literal that is only ever compared against
is left alone.

It also parses instead of grepping, and the reason is self-demonstrating: **the fixes leave the old
paths written down in the comments that explain them**, so a `grep '/tmp/'` gate fires on its own
justification — and the usual repair for that is a narrower pattern, which is how a gate ends up
asserting over prose. That is the M8 class this audit found live in `.dagger/charts.go`, where two chart
gates were matching YAML COMMENTS and passing on configuration that did not exist. Walking the AST means
`#` comments never enter it; docstrings are the one string form that does, and are skipped explicitly.

Three non-vacuity halves, all derived rather than remembered: the file count is checked against the same
`rglob` the scan uses (so a testpath that fails to resolve cannot read as a clean estate), the testpaths
themselves are read out of `pyproject.toml` rather than restated, and a planted-offence test asserts the
detector catches all three offending forms AND ignores the docstring, the bare literal and the mover
URI. RED proof: restoring the original `"/tmp/lance-test-root"` fires the gate naming file and line.

### M23 — a service conftest rewrites the environment for the whole session · **CONFIRMED, LOW**

`services/compute/tests/conftest.py:12-15` assigns `os.environ[...]` and eagerly imports the service at
**module scope**, so it rewrites the environment for every test in the `make test` session — including
tests that run before any compute test — and never restores it. The estate's last first-party conftest
doing this.

✅ **FIXED 2026-08-22** — `services/compute/tests/conftest.py` now saves the prior values of
`RAY_DASHBOARD_URL` and `RASK_API_PREFIX`, sets them only around the single `import compute` that needs
them, and restores them (deleting the key where there was none). Verified: 7 passed, and a probe in a
later testpath reports `RAY_DASHBOARD_URL present before=False after=False` — the variable no longer
outlives the import that needed it.

### L3 — two OTel tests leave live global exporters installed for the session · **CONFIRMED, LOW**

`packages/service-kit/tests/test_otel.py:18` and `:54` install real SDK Tracer/Meter providers with live
OTLP exporters aimed at localhost and tear nothing down. For the rest of the process the suite dials
`localhost:4317` with exponential backoff, and a **global HTTPX instrumentation** stays installed — so
every later outbound HTTPX request in the estate's suite silently gains a `traceparent` header it would
not otherwise carry. Visible in the CI log as `Transient error HTTPConnectionPool(host='localhost'…)`
interleaved with the sidecar failures.

✅ **FIXED 2026-08-22** — an autouse fixture in that file now RECORDS every `TracerProvider` /
`MeterProvider` / `LoggerProvider` constructed during a test and shuts each one down afterwards.
Recording rather than reading the globals is the whole fix, and the first attempt got it wrong: OTel's
setters are **set-once** (`set_meter_provider` "can only be done once"), so the global is whatever the
first `setup_otel` in the process installed, while every later call builds a provider that never becomes
global — yet still registers its reader in the SDK's class-level `MeterProvider._all_metric_readers`
WeakSet and still runs its export loop. Measured with a probe asserting no processor or reader is left
with `_shutdown is False`:

| variant | live exporters surviving the file |
| --- | --- |
| no fixture | `BatchSpanProcessor` + 3 × `PeriodicExportingMetricReader` |
| shutting down the globals (first attempt) | 2 × `PeriodicExportingMetricReader` |
| recording constructions (shipped) | **none** |

The lesson generalises past OTel: **a teardown that reads a set-once global disarms one object and
leaves the rest running**, and the log noise it removes (2 lines → 1 in the crude count that nearly
passed for proof) is far too weak a signal to tell the two apart.

### L4 — `build_settings()` reads an untracked developer `.env` and writes derived credentials into `os.environ` · **CONFIRMED, LOW**

`packages/service-kit/src/service_kit/__init__.py:70-71`. Permanently and unrestorably, and the one
fixture that claims to isolate it defers to whatever the ambient environment already says. Note also
that `.dagger/test.go`'s `WithDirectory` exclude list is `.venv, .git, node_modules, .dagger,
frontend/node_modules` — **`.env` is not excluded**, so a *local* `dagger call test` ships the developer's
`.env` into the container while CI, checking out fresh, does not.

✅ **FIXED + ENFORCED 2026-08-22 (the container half); the `os.environ` half MIGRATED.**

*The container half, measured live rather than reasoned.* `.env` exists on this host (1470 bytes) and
**none of the FOUR** Dagger build contexts excluded it — `main.go`, `test.go`, `charts.go` and
`frontend.go`, not just the one the finding names. All four now exclude it, and a probe counting `.env`
entries in the container's `/src` gives:

| | `.env` entries in the container |
| --- | --- |
| exclude removed (the pre-fix state) | **2** |
| exclude present | **0** |

Two — so the host carried a sibling `.env.*` as well, which is why the pattern is a pair.

The glob is `**/.env`, not `.env`, for a reason the estate has already learned once: a root-relative
pattern misses nested copies, exactly as bare `.venv`/`node_modules` in a `+ignore` leaves
`runners/*/.venv` behind. A service or runner growing its own `.env` is the same shape.

Enforced by `tests/unit/test_dagger_context_is_hermetic.py`, which parses every `Exclude:` literal in
`.dagger/*.go`, requires both patterns, checks the scan still reaches all four named contexts, and
asserts the patterns are recursive rather than root-only. RED: dropping the exclusion from `charts.go`
alone fails **exit 1** naming that file.

*The `os.environ` half is a design decision, not a patch.* `build_settings()` calls `load_dotenv()` then
`derive_hcp_creds()`, which writes derived credentials into the process environment permanently and
unrestorably — and the fixture that claims to isolate it defers to whatever the ambient environment
already says. Changing that changes how every service boots, so it is **MIGRATED to
`open_python-audit.md` § E14** rather than altered here. Excluding `.env` from the build context removes
the CONSEQUENCE this audit found (a local run diverging from CI on a secret-bearing input) without
pretending the underlying seam is fixed.

### What is clean

Worth recording, because an audit that only lists failures is less useful than one that says what holds:

- **Async wiring is disciplined.** AST scan of all 409 tracked test files: **521 tests marked
  `@pytest.mark.asyncio`, 25 marked `@pytest.mark.anyio`, and zero unmarked `async def test_`.** Zero
  plain `@pytest.fixture` on an `async def`. The classic silent-green is absent.
- **Both `xfail`s are `strict=True`** with long, accurate reasons. `test_ray_batch_e2e.py:86` pins an
  upstream lance-ray defect and explains why making it green would delete the capability it proves.
  Nothing *enforces* `xfail_strict`, but the convention is being followed by hand, correctly, twice.
- **`testpaths` is clean.** Every listed directory exists; the two suites that landed green-by-absence
  in 2026-08 (`services/catalog/tests`, `services/lineage/tests`) are in the list.
- **`tests/e2e-py/conftest.py` forces the `e2e` marker by location**, so a forgotten `pytestmark` cannot
  make an offline run hit a live endpoint. That is the right shape.
- **The cascade head is defended** (Part 4).

---

## Part 9 — Refuted, and the v1 refutations that still stand

Five findings were killed by the adversarial pass:

1. **`models-e2e-red-at-head`** — every fact checks out and the challenger reproduced the red exit, but
   the finding is v1's H4 restated; the increment it claimed over H4 does not exist. *Kept as H4's
   recurrence, not as a new finding.*
2. **`trash-expiry-exact-deadline-untested`** — the mutant survives (335 tests green, more broadly than
   filed) but it is an **equivalent mutant**: `deadline <= at` vs `deadline < at` differ only at exact
   nanosecond equality, and a test in a file the finder did not read contradicts the stated blast radius.
3. **`ray-job-image-gate-reads-a-non-deployed-dockerfile`** — the file:line is quoted correctly, but the
   mechanism that makes it a defect is false and **the proposed fix would delete a real gate.**
4. **`e2e-env-vars-assigned-nowhere`** — the causal mechanism fails a control experiment; two of five
   locations are the wrong lines; one is a 6× overcount ignoring an existing CI lane. *One sub-claim
   survives as M14.*
5. **`queue-health-tests-do-real-dns`** — killed by its own control case: `nats://rask-nats:4222` and
   `nats://127.0.0.1:1` were timed at **exactly the same cost**.

**v1's four refutations still stand** and must not be re-filed: `ci.yml` "web-e2e runs at most ONE
zone"; `test_fga_model_contract.py` "asserts a hand-written dict"; `test_invariants.py:1730` "Ray-address
check unreachable" (mechanism right, conclusion wrong); `dev-zone.test.ts:73` "emits no assertion".

Also **not** findings: `dev-zone.test.ts`'s annotator assertion (a known, real, unrelated red);
`frontend/packages/media-api` and `microfrontends/train|media` (untracked build residue on this host,
not workspace members).

---

## Part 10 — Status of every v1 item

| v1 | verdict | detail |
| --- | --- | --- |
| **H1** markers select nothing | **half closed, half cosmetic** | 13 targets now exist and select real markers. **No CI job calls any.** Targets exit 0 on all-skip. The gate that was supposed to keep it fixed accepts a Makefile target — and a comment — as an invocation site. → H18, M8 |
| **H2** auth-e2e 403 | **fix HOLDS, new defect introduced** | The dev-posture assertion is right. The nested-create it added uses `.` where the delimiter is `$`, so the grant is inert again. The **prod** posture is untested. → M10, H8 |
| **H3** nav-truth scanner | **HOLDS** | Frame walk in place, gate green, 45+45 hrefs asserted exactly. |
| **H4** models `test:e2e` | **STILL OPEN, actively recurring** | `Error: No tests found` is the live cause of `web-e2e` failing. `ci.yml:262-266` documents the harness as landed; it is untracked at HEAD, and untracked on this box right now. |
| **M1** `make test-slow` | **HOLDS in the Makefile** | Both legs collect. The gate that pins it is weak (M9), and neither target is in CI (H20). |
| **M2** runner suites in no CI job | **STILL OPEN, now camouflaged** | Unchanged, and `test_runner_suites_are_invoked.py` now makes the runner plane look covered. → H20, M9 |
| **M3** `mfe.spec.ts` | **fix HOLDS, overtaken by events** | The land-check landed. The chart defaults to `auth.enabled: true`, so it now skips all seven; and the `networkidle` wait on the same line can never fire. → M11 |
| **M4** transport-contract | **HOLDS** | Green; `createWarehouse` unified. |
| **M5** `test_gateway_e2e.py` | **STILL OPEN, worse** | Assertions still contradict the route table; the suite skips everywhere; `make e2e-gateway` now exists, so following the docstring runs three skips and exits 0. → M14 |
| **M6** DLQ poison-park | **fix HOLDS in source, fiction in the gate** | `ensure_dlq_stream` is at HEAD. The test proving it skips in every CI lane. → H19 |
| L: explorer `test:e2e:live` | **STILL OPEN** | One grep hit: its own declaration. |
| L: `dev-zone.test.ts` counts dirs | **STILL OPEN** | Unchanged. |
| L: `MIN_SUITE_FILES` | **WORSE** | Slack 1 → 2. → M7 |
| L: `no-networkidle` scan root | **WORSE** | Vacuous for 3 zones, not 2; the one live offender is still outside the root. |

**The pattern in that table is the finding.** Of six items closed in v1, four hold cleanly, one holds in
source but not in the gate (M6/H19), and one introduced a fresh defect while fixing the old one (H2/M10).
Of the four left open, three are unchanged and one was closed cosmetically in a way that *satisfied its
own guard*. **A gate written to keep a fix in place is itself a test, and it needs the same scrutiny as
the fix.** Three of the meta-gates written after v1 (M7, M8, M9) fail open.

---

## Part 11 — Documentation that will send the next engineer wrong

- **`testing-python` skill** (`ra-skills`, the skill CLAUDE.md routes *all* Python test work to) quotes a
  `testpaths` list of seven directories — **six of which do not exist** (`packages/htr/tests`,
  `components/services/core/tests`, …) — and states "`slow` is the only custom marker" when the root
  pyproject declares 15. Its single-test command errors out. An engineer following it writes a test into
  a directory the explicit `testpaths` list will never collect. It is a marketplace skill, so the fix
  belongs upstream in `AI-Riksarkivet/ra-skills`, not here.
- **`.claude/skills/rask-frontend`** — the zone-contract figures contradict themselves five lines apart
  and both are wrong; the known-red baseline names one failure while instructing the reader to check it
  is "still the *only* red." It is 8 tests across 3 files.
- **`Makefile:29`** asserts "the frontends have no unit suite" — contradicted by 125 tracked vitest files
  and 2,223 passing tests across the 13 packages that declare a `test` script.
- **`ci.yml:262-266`** documents the models e2e harness as landed (#112). It is not (H4).
- **`.dagger/test.go`**'s doc comment is *correct* and says the runners are not covered — the honest
  documentation of an open hole. Worth copying, not fixing.

---

---

## Part 12 — The gate plane itself: `.dagger/`, the chart gates, alerting, OpenAPI, lint

Twelve lenses swept the *tests*. None swept **the Go that implements every CI gate**, the chart render
chain, the alert fire-proof, or the lint gate's path scope. The completeness critic did, and it is the
richest single lens in the audit.

### H22 — `ms-charts` dies at step two, so the entire chart-gate chain has not run since 2026-08-04 · **CONFIRMED, HIGH, understated**

`.dagger/charts.go:103` is a bare `helm template chart >/dev/null`. Two chart guards refuse it and the
gate satisfies neither:

- `chart/values.yaml:387` ships `image.repository: ""` and `_helpers.tpl:833` `fail`s without a registry
  or `image.localImages=true` — landed **`3c909e0a`, 2026-08-04**, "registry required".
- `chart/templates/frontends.yaml:247` additionally fails without `frontend.oidc.sessionSecret` —
  landed `b6accf32`, "refuse a half-governed deploy".

Reproduced locally, independently of CI: `helm template chart` → **exit 1**. And `helm lint chart` on the
line *above* exits **0** while emitting the guard text as `[INFO]` — so the gate's first step is a green
light for a render that cannot happen.

**Everything behind step two has therefore never executed since 2026-08-04:** the NetworkPolicy isolation
invariants, the service-account hardening invariants, the Dapr resiliency/DLQ invariants,
`make prod-render-check` and `make alert-rules-check`. That is 923 commits of chart, security-context,
network-policy and alerting changes landing behind a gate that dies before it reaches them.

The challenger raised it further: **the dead gate is currently masking a live regression of the exact
defect it was built to catch.** Run it with the arguments it needs, and it does not merely pass — it
fails on something new.

And `tests/unit/test_invariants.py:401` cannot substitute, because `_helm_template()` always appends
`--set image.localImages=true` plus the three OIDC values: **all 13 pytest chart-render invariants render
the side-load path, and none renders the production registry path** (also filed as M6).


**FIXED 2026-08-22** (commit `028cb717`). `dagger call charts` exit 0, all five steps, for the first time since 2026-08-04. Six defects behind the dead step were fixed with it — see the tracker row for the list.
### M24 — six of fifteen alert rules have no fire-proof, and the semantic gate covers one group of seven · **CONFIRMED**

`chart/alerting/rules.yml:22, 34, 76, 193, 209, 226`. `promtool test rules` evaluates only the cases
`rules_test.yml` declares; a rule with no case is never touched and promtool exits 0. Enrollment diff: **15
alerts in `rules.yml`, 9 distinct alertnames in `rules_test.yml`.** `rules_test.yml` contains only
`alert_rule_test` blocks — no `promql_expr_test` — so there is no alternate path.

The one semantic guard, `test_invariants.py:3018`
(*"every maintenance ALERT names a metric the service actually EMITS"*), is scoped to **one of seven rule
groups** and one source file. So `medallion_stage_deniedTYPO_total` is a perfectly valid metric selector:
it passes `promtool check rules`, `promtool test rules` and all 105 chart invariants — while
`rules.yml:4-5` and `docs/DECISIONS.md:509-510` both assert the whole file is "proven to FIRE".

✅ **FIXED + ENFORCED 2026-08-22.** Both halves. Re-measured first, and the file had GROWN since the
audit: **20 alerts, not 15** — 14 with a fire-proof and the **same six** without.

*The proofs.* Ten `promtool` cases written for the six, each FIRING, and — where silence is a real
outcome rather than the absence of one — a paired case that must NOT fire, because a rule that pages
forever is its own outage. Enrollment is now **20 alerts, 20 tested, 0 missing**, and the cases bite:
breaking `max(outbox_oldest_age) > 300` to `> 99999` fails `make alert-rules-check` with **exit 2**
naming the alert. The expected labels and annotations are EXTRACTED from `rules.yml` rather than
hand-copied, so a proof cannot drift from the rule it proves by transcription.

`MaintenanceSweepNotCompleting` needed care the others did not: its rule is `== 0`, so the proof feeds
a FLAT counter rather than deleting the series — a missing series makes `sum(increase(...)) == 0`
vacuous instead of true.

*The scope.* The semantic gate is parametrized over every FIRST-PARTY group (lineage, medallion,
notifications, maintenance) instead of maintenance alone, and the three remaining groups are declared
third-party with whose exporter emits their series (`lance-catalog` rides `setup_otel`'s automatic HTTP
server metrics; `lance-infra`, `dapr-control-plane` and `ray` ride their own operators). A new group
landing in NEITHER list fails — RED: adding `lance-brandnew` fails naming it. The superseded
single-group function was deleted rather than left beside its replacement (32 lines).

**I reintroduced the exact bug this audit already caught once, and the mutation caught me.** The first
version of the widened gate used `[a-z_]+` for the metric name, so `medallion_stage_deniedTYPO_total`
matched as `medallion_stage_denied` — a real emitted metric — and the typo **passed**. That is the same
shape as the earlier `([a-z_]+)` gate-value pattern that skipped `can_be_notifiedX` rather than
rejecting it. Widened to `[A-Za-z0-9_]+`; RED then fires **exit 1** naming
`medallion_stage_deniedTYPO_total`. The general rule, now written at the code site: **a value pattern
narrower than the values it must reject will match a PREFIX of a bad value and call it good.**

121 invariants pass; `make alert-rules-check` exit 0.

*And per H22, `make alert-rules-check` sits behind the dead render, so none of it runs in CI anyway.*

### M25 — the CI lint gate is two directories narrower than `make lint` · **CONFIRMED, raised to MEDIUM**

`.dagger/checks.go:17-18` runs `uvx ruff check services tests` / `ruff format --check services tests` —
explicit paths, so ruff's discovery never walks the rest of the tree. `Makefile:60-61` runs
`uv run ruff check .`.

**Measured tracked LOC outside the CI gate: `packages/` 24,580 + `scripts/` 5,667 + `runners/` 8,997 =
39,244 lines** — five times the finding's original estimate. That includes all seven first-party libraries
and the `scripts/ray_*_job.py` entrypoints **baked into the cluster image**, which are production code.

Proved by mutation: appending `import os,sys` + `X  =  1` to `packages/storage/src/storage/__init__.py`
and the same to `scripts/ray_stage_job.py`, the CI gate's exact paths print nothing and exit 0 while
`make lint` reports both. **The local gate and the merge gate measure different estates, and nothing
asserts they agree.**


**FIXED 2026-08-22.** `.dagger/checks.go` now runs `ruff check .` and `ruff format --check .` — the same estate `make lint` walks — instead of `services tests`. That brought **39,244 tracked lines** onto the merge path: `packages/` 24,580 (all seven first-party libraries, including the authorization kernel), `scripts/` 5,667 (among them the `ray_*_job.py` entrypoints the cluster image BAKES, so production code) and `runners/` 8,997. Switched from `uvx ruff` to `uv run --no-sync ruff` in the same change: `uvx` resolves the LATEST ruff while the Makefile runs the LOCKED one, so widening scope on `uvx` would have traded one divergence for another — the estate has already been bitten by an unpinned `uvx ty` drifting until its hook blocked every commit. Verified: `ruff check .` and `ruff format --check .` both clean at HEAD.
### M26 — the OpenAPI contract covers 2 of 10 HTTP services, and its in-repo drift guard is name-only · **CONFIRMED, raised to MEDIUM**

`scripts/gen_openapi.py:33-36`'s `_SERVICES` is a hand-written two-row list (catalog, lineage), while
`.dagger/openapi.go:5-7` calls the result "the reviewable contract of **every** endpoint we serve." The
gateway, medallion, notifications, ingest, compute, viewer, search, annotator and flows all serve HTTP
and have no committed spec.

The sharper half: **`tests/unit/test_openapi_contract.py:42` is NAME-ONLY and passes green on the exact
drift that is currently reddening `ms-openapi`.** Reproduced by regenerating: `git diff` produces **+7
lines** on `docs/catalog-openapi.json` — the `accept_assertions` property on `PublishRequest`. The
in-repo guard compares operation names; the CI gate compares bytes; only the CI gate can see it, and the
CI gate is behind a red job.


**PARTIALLY DISPOSED 2026-08-22.** The drift half is a NO-OP — regenerated at HEAD, `git diff` empty (tracker row 5). The STRUCTURAL half stands and is NOT fixed: `_SERVICES` covers 2 of 10 HTTP services, and `test_openapi_contract.py` compares operation NAMES so it passes green on a byte-level drift only `ms-openapi` can see.
### M27 — the docs build has no merge-path caller, and it is currently red · **CONFIRMED**

`.github/workflows/docs.yml:14-17` triggers only on `workflow_dispatch` and `release: published` — no
`push`, no `pull_request`. `zensical build --clean` with `mkdocstrings-python` **imports** the modules it
documents, so it is a real (if narrow) import-health check, discovered at release time rather than on
merge. It also installs with `pip` in an estate whose toolchain rule is uv.

And it fails today: `PluginError: Could not collect 'storage.iiif'`, because `docs/reference/storage.md:20`
still references the IIIF read-through cache that **moved to `runners/htr` on 2026-08-17** — the move
CLAUDE.md records. Nothing on the merge path could have caught it.

✅ **FIXED + ENFORCED 2026-08-22 — and `storage.iiif` was one of THIRTEEN.** The gate found twelve more
the moment it existed, and they are all one mistake: `docs/reference/htr.md` (9 directives) and
`docs/reference/runner.md` (4) document a **sealed runner's internals** from the platform's own
reference. `runners/*` is matched by no workspace glob, so those modules are not importable from the
root environment at all — the pages could never have built since the seal.

They were **deleted, not repointed**, and that is the architectural answer rather than convenience:
CLAUDE.md rules that a runner's internals are deliberately undescribed at the platform level, and that
"there is no per-workload skill — one would make that modality look privileged, which is the opposite
of how this platform is built". `docs/index.md` pointed at `reference/htr.md` as THE API Reference, so
the platform's front door offered one workload's internals as its API — the identity rule failing in
the docs while it holds in the code. `storage.iiif`'s own section went for the same reason: the source
moved INTO a runner.

Two more the gate surfaced, both invisible with a release-only docs build:

* **`docs/reference/viewer.md` documented nothing** — a June-2026 tombstone for the dissolved viewer
  monolith, pointing at `core-api`, `orchestrator` and `services/core`, all since deleted, while
  `viewer` now means the lance media viewer on `:8101`. A page titled "API Reference — viewer"
  answering about a different `viewer` is worse than no page. Deleted.
* **Ten nav entries named pages that do not exist** — eight working documents (`OPEN-WORK.md`, three
  `DESIGN-*`, two dated assessments) plus the two runner pages. Each is a 404 in the built site.

**Gated rather than wiring the whole docs build into CI.** The build installs with `pip` in a uv estate
and belongs on its own decision; the IMPORT half — the part that actually caught a defect — costs
milliseconds on the merge path. `tests/unit/test_docs_references_are_importable.py` imports every
`::: dotted.path` and checks every nav target exists. RED: re-adding `::: storage.iiif` fails **exit 1**
naming file, line and module.

**My own non-vacuity floor was the magic-number trap this audit keeps finding.** I wrote
`len(references) >= 10` against the seventeen directives that existed BEFORE the cleanup, so it went red
on a change that made the estate more correct. Replaced with a derived one — every page under
`docs/reference/` must carry at least one directive — which is what immediately caught the empty
`viewer.md`.

### L5 — the `.dagger` Go plane has no Go-native formatting gate · **PARTIAL → LOW**

The finding as filed ("1,047 lines implementing every CI gate, tested by nothing") had its three
load-bearing claims refuted — `.dagger/go.mod` *is* enumerated by osv-scanner (`scan.go:25`), and a
`dagger call` does compile and type-check the module. What survives is narrow and true: **there is no
`gofmt -l` gate**, and `gofmt -l .` over `.dagger` reports two tracked hand-written files unformatted
today (`images.go`, `main.go`).

✅ **FIXED + ENFORCED 2026-08-22** — `dagger call go-fmt` (`.dagger/checks.go`), reachable as
`make go-fmt` under the same one-definition contract as `make audit`. The two files are formatted;
gofmt's changes are entirely doc-comment indentation, which is what gofmt has normalized since 1.19.

**It runs in a container, and that is not a convenience.** There is no Go toolchain on the developer
PATH here, and the repository rule is absolute — every container goes through Dagger, no exceptions,
including one-off tooling. So the gate ships with a paired writer, `dagger call go-fmt-fixed export
--path=.dagger`, because a gate whose only remedy is "install a toolchain" is a gate people work
around, and the Python plane has had both halves (`make fmt` / `ruff format --check`) the whole time.
This was never a policy decision about Go; it was a plane nobody pointed a gate at.

One detail worth keeping: `gofmt -l` prints the offenders and **exits 0 either way**, so wrapping it in
a container step proves nothing on its own — the same shape as a piped command masking its status,
which this audit already caught once in its own evidence. The step turns a non-empty list into exit 1.
RED: appending a misindented doc comment to `main.go` gives **exit 1** naming the file; GREEN prints
`gofmt: clean`.

Scope stated: `make go-fmt` sits beside `audit`/`scan-config` rather than inside `check`, matching the
estate's existing split for engine-dependent gates, so it is not yet on the merge path — that needs a
`.github/workflows/ci.yml` edit, and that file is held by a concurrent session in this tree.

*Recording the refutation matters more than the survivor here.* "The plane that implements the gates is
itself ungated" is a seductive claim and it is mostly wrong — but H22 and M24 are real, and they are what
the seductive version was reaching for.

### L6 — `chart-toggle-off` assertions are a latent vacuity, not an active one · **REFUTED → LOW**

Filed as HIGH: `off=$(helm template chart … | grep -c "kind: X" || true); [ "$off" = "0" ] || exit 1`
puts the `|| true` **outside** the pipeline, so `set -euo pipefail` cannot propagate a render failure into
the substitution. I reproduced the shell mechanism directly:

```
$ bash -c 'set -euo pipefail; off=$(false | grep -c "kind: NetworkPolicy" || true); [ "$off" = "0" ] && echo PASSED'
PASSED    # on a render that produced nothing
```

The challenger correctly cut it down: two of the four named sites do not have that shape
(`charts.go:177` redirects to a file and is checked separately), and the other two **cannot produce a
green gate today** — the bare render at `:103` has no `|| true` and kills the run first. So this is a
**latent hazard at two sites** (`:120`, `:132`), not an active vacuity. It becomes active the moment H22
is fixed by making step two tolerant rather than by giving the render its arguments.

✅ **FIXED 2026-08-22 — and the trigger the finding predicted is exactly what happened.** H22 *was*
fixed the right way (the render got its arguments, `renderArgs`), which means the bare render at `:103`
no longer kills the run first — so the two latent sites became reachable. Filed as latent, closed as
real, because the thing that was holding them harmless was removed.

Both now render to a FILE before asserting an absence, which puts the render on its own command where
`set -e` sees its status, plus a `kind: Deployment` probe as the non-vacuity floor — proving the render
produced a real manifest before anything concludes something is absent from it. Nothing is asserted
about a document nobody checked exists.

Three-way shell proof, since the discrimination is the whole point:

| shape | input | result |
| --- | --- | --- |
| old (`\| grep -c … \|\| true`) | a render producing nothing | **exit 0 — PASSED**, the vacuity |
| new (file + floor) | a render producing nothing | **exit 1** |
| new (file + floor) | a real manifest with no NetworkPolicy | **exit 0 — PASSED**, correctly absent |

The third row matters as much as the second: a fix that failed on a genuine absence would have replaced
a false green with a false red. `dagger call charts` **exit 0** with the hardened gates.

### N1 — the docker prohibition is enforced by nothing · **NEW, found 2026-08-22 · HIGH**

Found while fixing M12: the adjacent `notifications-rig-up` target runs `docker compose`. A survey
found **47 docker invocations** across `Makefile`, `scripts/` and `.github/workflows/`.

`CLAUDE.md` states this rule three times, escalating each time, and records it being violated once —
the build-only scoping "was read (2026-08-15) as licence to `docker run` a throwaway NATS for a test
repro." **Nothing gated it.** The frontend plane has had `toolchain.test.ts` failing the build if
ESLint or Prettier reappear the whole time; the rule the repository calls non-negotiable had no test.

✅ **ENFORCED + MIGRATED 2026-08-22.**

*Enforced* — `tests/unit/test_no_docker.py`, two tiers because the rule genuinely has two:

* **Tier 1, docker BUILDS an image.** Absolute, no exceptions, and CLAUDE.md names the files. **It
  passes with an EMPTY exemption list** — `docker build`/`buildx` appears nowhere. The estate's hardest
  clause holds today; it simply had nothing keeping it true.
* **Tier 2, docker CREATES a container.** Three sites, none a bootstrap, kept as a **shrink-only
  roster** rather than an exemption list — the roster fails if a fourth appears AND fails when one is
  fixed, forcing the entry deleted rather than left as folklore. Two genuine bootstraps are exempted
  permanently and by name: you cannot use Dagger to create the Dagger engine, and the registry Dagger
  pushes to must exist before a push can reach it.

Three RED proofs: a new `docker build` fires tier 1; a fourth container site fires the roster naming
it; and *fixing* `Makefile:463` while leaving it listed fires with `fixed (delete from
_KNOWN_VIOLATIONS)`. 5 passed.

What it deliberately does not flag: `docker inspect/load/tag/pull/save` and `command -v docker`. Those
talk to an existing daemon about images Dagger built — image plumbing, not container creation — and
`scripts/dagger-image.sh` is the seam CLAUDE.md names as the correct path for exactly that. A gate that
fired on the sanctioned route is a gate someone deletes.

*Migrated* — the conversion of the three sites is a scope decision, not a patch: every one is a
DETACHED dev convenience (`up -d`) and `dagger core … as-service up` holds a terminal, so the dev loop
changes and two compose files retire with it. Written up as **`open_python-audit.md` § E13** with the
per-site table and the prescribed `dagger core container` pattern. Deleting this audit loses nothing.

---

## Part 13 — Two caveats on this audit itself

**A concurrent session was editing this working tree throughout.** HEAD moved `47dba152` → `9e4bd084` →
`67e7806f` mid-run. Two transient reds were observed and are deliberately **not** filed as findings:
a nine-failure burst in `tests/unit/test_medallion_cascade.py` that did not reproduce on three subsequent
runs, and `tests/unit/test_dummy_quality_gate.py::test_the_restated_schema_matches_the_sealed_runners_own`,
which fails on the pristine tree now and passed twenty minutes earlier. **Someone should confirm that
second one is their in-flight work and not a landed regression.**

**What no lens reached.** Stated so the next audit knows where to start: the frontend gate was exercised
on the host toolchain rather than inside `dagger call frontend`, so a container-only divergence is
untested; no suite that writes to the live k3s cluster was run (`dummy_lane`, `user_state`, `medallion`,
`media`, `observability`, `governed_union` — their status is established statically); `make test-slow`,
the runners' own suites and any Dagger/k3s bring-up were not run; and turbo's cache was warm, so the
*exact* set of tasks skipped by fail-fast is cache-dependent even though the mechanism is not.

**A methodology gap, recorded so the next pass does better.** `open_python-audit.md` set the bar for
this repo: *"Every skill reference was read in full **before** any code was opened … compiled into a
17-rule calibration sheet that every auditor was held to."* **This audit did not meet that bar.** Two
skills were read up front (`testing-python`, `rask-frontend`); `writing-python/references/testing.md`
was handed to two lenses as a path rather than read by the author, and no calibration sheet was
compiled. Reading it afterwards immediately produced **M19b** — a HIGH that all twelve lenses missed,
because none of them had the rule it states. Assume there are others of that shape, and read the
references first next time.

**One more structural gap worth its own line.** `tests/unit/test_invariants.py:1598`
(`test_every_workspace_test_directory_is_in_the_root_testpaths`) globs `packages/*/tests` and
`services/*/tests` in both directions — good. It does **not** glob a new top-level `tests/<x>/`, which is
exactly how `tests/e2e-py` was lost once before.


## What I would do first

**1. Fix one file and four CI lanes come back.** Mock `typed_proxy` in
`tests/unit/test_annotation_task_actor.py` the way its thirty siblings do. That un-reds `ms-test`,
un-skips `e2e-stack` / `e2e-ray` / `e2e-lineage` / `e2e-auth`, un-hides the `F` at 41 %, and removes the
ordering coupling that makes an unrelated notifications test green by accident. Nothing else in this
document unlocks as much.

**2. Get CI back to green at all, then stop it failing fast.** `rsvelte-fmt .` on the lakehouse;
regenerate the OpenAPI spec; commit or delete the models e2e harness; give `.dagger/charts.go`'s render
its arguments. Then add `--continue` to the two turbo invocations, so the next red gate reports
*everything* that is red rather than the first thing. **A gate that reports one failure of thirty-five is
not a gate**, and today both the JS gate and the chart gate are exactly that.

**3. Give the chart gate its arguments — and expect it to stay red.** H22 is the quietest large finding
here: `.dagger/charts.go:103` has been unsatisfiable since 2026-08-04, so the NetworkPolicy isolation
invariants, the service-account hardening invariants, the Dapr resiliency/DLQ invariants,
`make prod-render-check` and `make alert-rules-check` have all sat behind a dead step for 923 commits.
Fix it by giving the render `image.localImages=true` (or a registry) **and** `frontend.oidc.sessionSecret`
— not by making step two tolerant, which would activate the latent vacuity in L6. The challenger's
warning is the part to plan for: run it properly and it fails on something *new*.

**4. The authorization cluster is the largest real exposure.** H6–H10 and M1–M2 are one root cause wearing
six hats: **the model's tests pin names and samples, not derivations.** A widening, a phantom relation, a
tier downgrade and an unasserted grant axis are all invisible today. Adding derivation-level assertions to
`model.fga.yaml` is more valuable than every LOW in this document combined, and `test_invariants.py`'s
phantom scanner needs to reach lineage, notifications, viewer and flows before it can back any of it up.

**5. Decide whether the notification producers are a contract or a convention.** `lance.project` and
`lance.originator` can be deleted from the shipped mover with zero red. The skill says an event that
names nobody is *undeliverable and acked SUCCESS*, which means there is no runtime signal either. Either
those fields get producer-side assertions, or the skill's four traps are documentation of a hazard nobody
guards.

**6. The three structural holes worth naming out loud** — `services/search` has no `tests/` directory at
all (2,614 tracked LOC, absent from `testpaths`); seven of nine sealed runners have neither tests nor any
CI-reachable check (and six of those seven have no `uv.lock` either); and 39,244 lines under `packages/`,
`scripts/` and `runners/` are lint-checked by `make lint` but by no merge-path gate. All three are scope
decisions, not oversights to fix in an afternoon. They belong in a plan, not a patch.

Nothing here means production is broken. It means the suite's green is worth less than it looks, the
estate's red has been unread for six days, and the gap is widest exactly where the estate has written
down that correctness matters most.
