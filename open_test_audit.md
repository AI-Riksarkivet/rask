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
| 6 | Give `.dagger/charts.go:103` its render arguments | H22 | 🟨 **PARTIAL 2026-08-22** — shared `renderArgs` added; **all 3 invariant gates now PASS** for the first time since 2026-08-04. Running them surfaced 4 defects, all fixed: policy moved `constant`→`exponential` under the dead gate; a service account became unconditional; **2 gates were matching YAML comments, not config** (the M8 class, live). Still red: `prod_render_check.sh` (same `image.repository` guard) and `make alert-rules-check` (bare `promtool` not on PATH — and `.dagger/charts.go:44`'s claim that the Makefile exports `$(LOCALBIN)` on PATH is **false**; `git log -S` finds no such line, ever) |
| 7 | `assert_all_called=True` on respx | M19b | ⬜ **expect red** — 118 bare `@respx.mock`; the redness is the finding |

### Tier 2 — after CI is green

| | item | finding |
| --- | --- | --- |
| 8 | Identify the **`F` at 41 %** on the first green `ms-test` | H1 (open thread) |
| 9 | Drain-state pass over `open_python-audit.md`, then **merge** into one backlog | Part 13 |
| 10 | Then its waves: E1/E2 → E3/E4 → E5/E6 → E7/E8/E10/E11 → E12 — guard clauses and deletions **last** | that audit's own execution order |

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

### H4 — `@rask/api`'s liveness test has never run a single assertion · **CONFIRMED, MEDIUM**

`frontend/packages/api/src/live.svelte.ts:1` imports `svelte`, which `@rask/api` **has never declared
in any dependency field**. `live.test.ts` therefore fails at module load with **0 tests collected** —
and has done since it landed on 2026-08-05. It also takes `web-gate` down with it, which is how it
stayed invisible: the job was already red.

### H5 — `docs-roster.test.ts` cannot execute in the container it ships in · **CONFIRMED, MEDIUM**

`frontend/packages/zone-contract/src/docs-roster.test.ts:27` shells out to `git ls-files` to derive
the zone roster. The frontend gate runs in `oven/bun:1.3.14-slim`, which **has no `git` binary**, and
`.dagger/frontend.go` additionally strips `.git` from the copied source. **4 of its 6 tests cannot run
in CI at all** — they pass locally and are structurally absent from the gate.

---

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

### H7 — `NOTIFY_RELATION` can be replaced with a relation that does not exist · **CONFIRMED, HIGH**

`services/notifications/src/notifications/api/visibility.py:60`. Mutating it to `can_be_notifiedX` —
a relation the model does not define — leaves **769 tests passing, 0 failing** across
`services/notifications/tests`, `test_invariants.py` and `test_fga_model_contract.py`.

In production that fails closed and **silences the entire inbox plane**. The relation that gates every
notification delivery in the estate is guarded by nothing.

### H8 — the production `lockRootCreate` posture is untested · **CONFIRMED, HIGH**

`services/catalog/src/catalog/api/fga_deps.py:273-274`. `chart/values-prod.yaml:22` ships
`lockRootCreate: true` — the only thing stopping any authenticated token from minting top-level
namespaces and tables in production. That branch can be **silently downgraded from the writer-tier
`can_create_*` to the reader-tier `can_get_metadata`** and the whole catalog + unit + integration
suite stays green (**3,001 passed**).

v1's H2 established that the estate has two postures, one per environment. It fixed the test to assert
the *dev* posture. **Nothing asserts the prod one.**

### H9 — the phantom-relation scanner reaches 10 of the estate's relations · **CONFIRMED, raised to HIGH**

`tests/unit/test_invariants.py:316-342`. `_fga_literals()` is the estate's only repo-wide guard against
a service checking a relation the model does not define. Its regexes are literal-only and
whitespace-sensitive, so across all of `services/` it resolves **10 distinct (type, relation) pairs —
zero from lineage, notifications, viewer or flows.** Proved by full-suite mutation: a phantom on the
notifications delivery gate leaves **4,849 tests green**. This is the mechanism behind H7.

### H10 — the `table` rung's entire grant axis has no assertion · **CONFIRMED, raised to HIGH**

`model.fga:362-382`. Six `can_*` relations carry no assertion of any kind in `model.fga.yaml`; five of
them are the **`table` rung's whole grant axis** — the finest-grained and by far the most numerous
governed object in the estate. The challenger raised this from MEDIUM on reachability grounds:
`_GRANTABLE_BASE` in `access.py:82` makes every one of them reachable from a real grant call.

### M1 — `fga model test` runs in exactly one place, and no `make` target is it · **CONFIRMED, raised to MEDIUM**

The OpenFGA model's evaluation semantics are guarded by a single line: `ci.yml:208`. `make ci` (=
`check` + `test`) can be fully green on a machine where the authorization model's own suite has never
executed. Given Part 0, "runs only in CI" is currently a synonym for "runs when ms-authz happens to be
one of the four green jobs."

### M2 — `fga.list_objects` is exercised by no test · **CONFIRMED**

`model.fga.yaml:450`. `list_objects` builds the allow-list behind every table listing in the catalog —
it is the control that prevents cross-tenant table disclosure. It has **no `list_objects` row in the
model tests and no unmocked Python test anywhere.** The recursive upward-visibility edge the warehouse
listing actually enumerates is covered by neither of the two rows that do exist.

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

### H12 — `POST /produce`'s 503 tail is executed by no test · **CONFIRMED, HIGH, and understated**

`services/medallion/src/medallion/api/produce.py:77-90`. The route's own docstring makes the contract
load-bearing verbatim: the bronze-write emit is the cascade head, "so a publish failure surfaces as
**503** (not the 202 that would hide it), letting the caller retry."

Line coverage: `77-90` missing. The `publish_failed` check, the 503 problem+json with `Retry-After: 5`,
and the 202 return **never execute**. The one test that reaches the route asserts only
`status_code != 403` against a handler deliberately wired to blow up.

The challenger found it is **three routes, not one** — the identical unguarded 503 branch sits at
`ingest_media.py:64-75`, whose entire handler body is at 46 % with `37-76` missing.

### M3 — the mover's `originator` is guarded by nothing · **CONFIRMED, MEDIUM**

`transform.py:550, 623, 651, 686, 725, 773` and `:802` — seven sites, not six. All can be deleted with
no attributable failure (the challenger ran a clean baseline of the identical command to prove the one
red was pre-existing — the ordering flake from §H1.3).

Why it matters: the mover authors with a **chart role literal** (`MEDALLION_AUTHOR` = `data_eng` /
`analyst` / `ray`), so `author_subject()` addresses an inbox actor named `data_eng` — nobody.
`lance.originator`, carried from `/produce`'s verified sub through `/bronze-arrival`, is the **only**
way a failed cascade run reaches the human who started it. That is trap 2, and nothing holds it.

### M4 — `request_approval` is executed by no test · **CONFIRMED**

`services/medallion/src/medallion/workflow.py:816-842`. The sole producer of
`promotion_review_requested` — the reason that asks a named person to decide a held promotion — never
runs: the `CatalogControlEvent(...)` construction, the `_publish()` closure and the success log are all
reported missing. Two suites *appear* to cover it and each covers the other half:
`test_promotion_review.py` stubs the activity.

The `extra["subject"]` that **is** the targeting is unverified end to end.

### M5 — the media DROP path's FAIL emit never runs, inside a `suppress` · **CONFIRMED**

`transform.py:666-704`. The `UnderivableMediaError` branch returns `_DROP`, so Dapr will **not**
redeliver — as the code's own comment says, "a lost FAIL publish means the failed run is NEVER recorded
and NEVER retried: the graph silently forgets it." The FAIL emit is the compensating control. It is
executed by no test **and** it is wrapped in `with suppress(Exception)` at `:676`, so a defect inside it
produces silence rather than a red.

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

### H14 — the authn-audit compliance gate is a file-wide substring count · **CONFIRMED, HIGH**

`tests/unit/test_invariants.py:729` is `assert src.count("audit(") >= 2` over the whole of
`security.py`, which currently contains **10** audit calls. Its docstring claims the gate proves
`authenticate` audits both the success and the failure paths. **Eight of the ten — including the
SUCCESS audit on the service-credential door — can be deleted and the gate still passes.**

### H15 — the BFF-caller gate is satisfied by a doc comment · **CONFIRMED, HIGH**

`frontend/packages/zone-contract/src/bff-routes.test.ts:99`. The gate that proves every BFF proxy route
has a caller does `sources().filter(f => f.text.includes(pattern))` across the **whole** frontend tree.
So a **doc comment mentioning the path**, another zone's file, or an e2e mock that fakes the route all
count as callers. Proved under mutation.

### H16 — the estate-navbar zone roster is a hand-written literal · **CONFIRMED, MEDIUM**

R15 ("a zone missing from the shared navbar is a defect regardless of scaffold status") is guarded only
by a **hand-written six-title array** inside `@rask/ui`'s own test (`nav-config.test.ts:32`). No gate
anywhere compares the estate navbar against the zone directories — yet the test's own comment claims
"a zone scaffolded without an entry fails here." It would not.

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

### M7 — `MIN_SUITE_FILES` slack grew from one suite to two · **CONFIRMED, LOW→MEDIUM**

`tests/unit/test_e2e_collection_gate.py:54` pins `MIN_SUITE_FILES = 24` against **26** suite files that
collect today. Two suites — and the challenger notes the registry-CAS *and* governed-union suites are
both candidates — can stop collecting entirely while all four assertions in the gate report green. That
is the exact failure the constant's own comment says must not exist, at a scale of two.

### M8 — the marker gate is satisfied by a comment · **CONFIRMED**

`tests/unit/test_e2e_collection_gate.py:169` greps the **raw text** of `.dagger/*.go`, `scripts/*.sh`,
`Makefile` and `.github/workflows/*.yml` for `-m <marker>` — comments included. Deleting a real
`make e2e-<suite>` target and leaving a `# TODO: pytest -m media` comment behind satisfies it. The
`media` marker's site today *is* prose.

### M9 — the runner-invocation gate passes on a leg that selects zero tests · **CONFIRMED, LOW**

`tests/unit/test_runner_suites_are_invoked.py:85-95` — written to keep v1's M1 fixed — asserts only
that a Makefile recipe line mentions `runners/<r>` and `pytest` and starts with `cd runners/<r>`.
`wired` is a substring match and the `cd` check is `startswith`, so nothing constrains what follows
`pytest`: a leg narrowed to select nothing passes. It is also **silent about the seven of nine sealed
runners that ship no tests at all**, because it enumerates only runners that already have a `tests/`
directory.

### Smaller gate-reach findings (all confirmed, LOW)

- **`view-transition.test.ts:28`** — the gate whose docstring says "this is the gate that keeps
  [the cross-document at-rule] out" reads **one stylesheet of eleven**, and none of the seven zone
  `app.css` files a person chasing the cross-zone flash would edit. Repo-wide grep finds no second gate.
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

### M10 — the live auth suite is selected by nothing, and the job named `e2e-auth` does not run it · **CONFIRMED**

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

### M11 — the standalone browser suite skips every test on any default deploy · **CONFIRMED, HIGH→MEDIUM**

v1's M3 correctly stopped `tests/e2e/tests/mfe.spec.ts` from counting an OIDC bounce as a pass. It did
so with `test.skip`. **The chart has shipped `auth.enabled: true` by default since 2026-08-06**, so all
seven route tests now skip against any default estate and `make e2e` exits 0 having exercised no zone.
The escape hatch the skip message offers is unimplemented: `tests/e2e/playwright.config.ts` has no
`globalSetup` and no `storageState`.

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

The ORIGINATOR lane — the targeting source that is *structurally unreachable* from a unit test, and
whose producer-side field M3 shows is unguarded — is proven only when a human remembers to type
`node tests/e2e/verify_originator_lane.mjs`.

### M13 — `make e2e` is in no CI job · **CONFIRMED**

`Makefile:769-770`. Grep for `make e2e` not followed by `-` across `.github/workflows/`: no match.

### M14 — five e2e suites are gated on env vars nothing assigns · **PARTIALLY REFUTED — see Part 9**

The lens filed five; the challenger's control experiment killed the causal claim and found two wrong
line references and a 6× overcount. **The surviving core:** `tests/e2e-py/test_gateway_e2e.py` is a dead
file — nothing in the repo sets `LANCE_E2E_GATEWAY_URL`, so its three tests skip everywhere, and its
assertions at `:41, :48, :51, :57` contradict the current gateway's route table anyway. That is v1's M5,
still open, and now **worse**: the `make e2e-gateway` target its docstring cites now *exists*, so
following the docstring runs three skips and exits 0.

### M15 — `home#test:e2e` is red at HEAD independently of everything else · **CONFIRMED, HIGH**

`frontend/microfrontends/home/playwright.config.ts:98-100`. The auth-OFF `chromium` project matches
`/\.spec\.ts$/` and ignores only `e2e/(projects|settings)/`. `e2e/notifications/watch-enrolment.spec.ts`
is therefore collected **a second time**, against the wrong dev server, where all 3 of its tests fail —
while the `chromium-notifications` project at `:135-138` collects the same files correctly.

### M16 — the lakehouse warmup project matches a directory that does not exist · **CONFIRMED**

`frontend/microfrontends/lakehouse/playwright.config.ts:126` is
`testMatch: /e2e\/(data|lineage|storage)\/warmup\.setup\.ts/`. The zone has **no `e2e/data/`** and does
have `e2e/catalog/`, so `e2e/catalog/warmup.setup.ts` is collected by nothing and the catalog area runs
against the cold Vite cache the config exists to pre-warm.

### M17 — the Postgres tracker backend is exercised by nothing · **CONFIRMED, LOW**

`packages/tracker/tests/test_postgres.py:179-180` gates six integration tests behind a
`--postgresql-port` CLI option **no Makefile target, script, CI job or Dagger function passes**. The
package's stated contract is backend-agnosticism; its production backend has no runner.

---

## Part 7 — Mocks and doubles that stand in for something else

### M18 — the lakehouse's own dev seed describes an API the catalog does not serve · **CONFIRMED**

`frontend/microfrontends/lakehouse/e2e/dev-seed.ts:37-47`. Three of the four catalog bodies describe
routes/shapes the catalog does not serve — `namespaces.py:54` mounts `/v1/namespace` with only
`{id}/…` subpaths and no bare GET — and **two of them provably fail the zone's own valibot schemas**.
Reproduced against the real launcher: `make dev-zone ZONE=lakehouse` renders the warehouses and
storage-tier pages as **502 "catalog contract drift"** — the exact broken state the seed file exists to
prevent.

### M19 — eleven register-path tests mock a call the estate ruled must never happen · **CONFIRMED**

`services/medallion/tests/test_register_uses_the_service_door.py:32` (and `test_catalog_register.py:56,
65, 79`). Eleven tests still register a respx mock for `POST /v1/namespace/{tier}/create` — a call the
cascade must never make — answering 200/409, statuses the real catalog cannot return for it. **If the
regression came back, 2 of 13 tests would notice and the other 11 would absorb it.**

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

### M20 — the lakehouse's two session-bearing data-plane routes are mocked away everywhere · **CONFIRMED**

`microfrontends/lakehouse/src/routes/capi/v1/table/[id]/query/+server.ts:16-41` and its `insert`
sibling. Every spec that names their contract intercepts them at `page.route`, and no unit test covers
them. Proved by mutation: replacing **both handler bodies** with `return json({detail:'AUDIT MUTANT'},
{status:500})` — no auth gate, no clamp, no bearer forwarding, no upstream call — left the three specs
**54/54 green**, including one titled "the preview…".

So the anonymous-401 confused-deputy gate, the bearer forwarding, the `MAX_PREVIEW_ROWS` clamp and the
`{k, vector:{}}` body the catalog actually receives are asserted by nothing.

### M21 — eleven of fourteen gateway rewrites are pinned to hand-written URLs · **CONFIRMED**

`services/gateway/tests/test_lance_routes.py:53-74`. The `MockTransport` at `:30-34` returns 200 for any
request, so the only upstream check is `str(captured[-1].url) == <literal>`. A rewrite landing on a path
the upstream does not serve passes — and the viewer row's own example, `/api/transcripts`, **is not among
the 33 paths the viewer actually serves.** This is the assertion shape the ingest row already passed
while every `/api/ingest/*` call 404'd in production.

### L1 — two green suites assert opposite things about Ray job metadata · **CONFIRMED, LOW**

`frontend/packages/api/src/ray.test.ts:51-57` asserts `metadata` survives the wire; the backend's own
test asserts the service strips it. Both green. The gate that claims to catch exactly this drift is a
hard-coded literal set that cannot fail.

### L2 — the explorer's mock detects its envelope by key presence · **CONFIRMED, LOW**

`explorer/e2e/mock-media-services.ts:71` uses `'status' in h`, the exact form all three sibling mocks
fixed after it made every seeded route answer HTTP 500 when the payload carried its own string
`status` field.

---

## Part 8 — Test infrastructure

### M22 — the integration client fixture writes to a fixed shared path · **CONFIRMED, raised to MEDIUM**

`tests/integration/conftest.py:64` points the catalog's root at a hard-coded `/tmp/lance-test-root`
instead of `tmp_path`. Real catalog state accumulates across runs, across **concurrent** runs, and
across users on the same host.

### M23 — a service conftest rewrites the environment for the whole session · **CONFIRMED, LOW**

`services/compute/tests/conftest.py:12-15` assigns `os.environ[...]` and eagerly imports the service at
**module scope**, so it rewrites the environment for every test in the `make test` session — including
tests that run before any compute test — and never restores it. The estate's last first-party conftest
doing this.

### L3 — two OTel tests leave live global exporters installed for the session · **CONFIRMED, LOW**

`packages/service-kit/tests/test_otel.py:18` and `:54` install real SDK Tracer/Meter providers with live
OTLP exporters aimed at localhost and tear nothing down. For the rest of the process the suite dials
`localhost:4317` with exponential backoff, and a **global HTTPX instrumentation** stays installed — so
every later outbound HTTPX request in the estate's suite silently gains a `traceparent` header it would
not otherwise carry. Visible in the CI log as `Transient error HTTPConnectionPool(host='localhost'…)`
interleaved with the sidecar failures.

### L4 — `build_settings()` reads an untracked developer `.env` and writes derived credentials into `os.environ` · **CONFIRMED, LOW**

`packages/service-kit/src/service_kit/__init__.py:70-71`. Permanently and unrestorably, and the one
fixture that claims to isolate it defers to whatever the ambient environment already says. Note also
that `.dagger/test.go`'s `WithDirectory` exclude list is `.venv, .git, node_modules, .dagger,
frontend/node_modules` — **`.env` is not excluded**, so a *local* `dagger call test` ships the developer's
`.env` into the container while CI, checking out fresh, does not.

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

### M27 — the docs build has no merge-path caller, and it is currently red · **CONFIRMED**

`.github/workflows/docs.yml:14-17` triggers only on `workflow_dispatch` and `release: published` — no
`push`, no `pull_request`. `zensical build --clean` with `mkdocstrings-python` **imports** the modules it
documents, so it is a real (if narrow) import-health check, discovered at release time rather than on
merge. It also installs with `pip` in an estate whose toolchain rule is uv.

And it fails today: `PluginError: Could not collect 'storage.iiif'`, because `docs/reference/storage.md:20`
still references the IIIF read-through cache that **moved to `runners/htr` on 2026-08-17** — the move
CLAUDE.md records. Nothing on the merge path could have caught it.

### L5 — the `.dagger` Go plane has no Go-native formatting gate · **PARTIAL → LOW**

The finding as filed ("1,047 lines implementing every CI gate, tested by nothing") had its three
load-bearing claims refuted — `.dagger/go.mod` *is* enumerated by osv-scanner (`scan.go:25`), and a
`dagger call` does compile and type-check the module. What survives is narrow and true: **there is no
`gofmt -l` gate**, and `gofmt -l .` over `.dagger` reports two tracked hand-written files unformatted
today (`images.go`, `main.go`).

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
