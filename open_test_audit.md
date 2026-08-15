# open-test-audit — which tests are green without testing anything

Ran 2026-08-15. **25 agents: 7 hunters over the whole estate, then one adversarial challenger per
candidate.** 18 candidates → **14 survived**, **4 refuted**. Every survivor was reproduced by the
challenger independently — several by RUNNING the thing (starting a NATS container, rendering the
chart across five value sets, executing the vitest gate with a mutated input), not by reading it.

The question was not "does this test pass" but **"if the implementation were replaced with `pass`,
would this test still be green?"** That framing is what found most of this.

Prompted by diff2 F5, which was the archetype: the tenant-isolation e2e read
`body["credentials"]["aws_access_key_id"]` against a payload that has never had that shape. It was
wrong from the day it was written and stayed green for its whole life, because it is env-gated and
skips. **A test that never runs cannot fail.**

---

## The shape of the rot

Almost none of it is "an old test for deleted code" — that hypothesis was checked first and came back
nearly empty. `testpaths` is clean (18/18 valid, zero orphaned test dirs), and the estate has been
disciplined about deleting tests with their subjects.

What is actually wrong is subtler and worse:

| pattern | count | why it survives |
| --- | --- | --- |
| **Green by not running** | 5 | env-gated on a var nothing sets; the suite skips and reports success |
| **Floors that absorb the loss** | 4 | a scanner silently matches less than it should, and the `> 30` guard has slack |
| **Invoked by nothing** | 3 | a `test:e2e:*` script or make target with no caller — never passes, never fails, never appears |
| **Asserts a contract that moved** | 2 | the code changed; the skipping test still asserts the old answer |

The recurring mechanism: **a guard that measures the wrong quantity.** A floor on
"how many zone directories exist" cannot notice a scanner that reads none of them. A canary asserting
"the name parsed" cannot notice that the comparison never happened.

---

## HIGH

### H1 — `pyproject.toml:208-224` · 12 e2e markers select nothing; 11 suites run nowhere

The 12 per-suite markers (`cas`, `governed_union`, `gateway`, `medallion`, `media`, …) are declared
with a comment saying they are "used by the e2e make targets (e.g. `pytest -m media`)". **No target,
script or CI job selects any of them.** 11 of the 25 live suites (29 tests) are consequently
executed by nothing at all.

Hides behind `test_e2e_collection_gate.py`, which is green and reads as proof the plane is intact —
but it only asserts the files *collect*. It has no notion of a suite that collects and is then run by
nobody, and its dead-path regex checks file paths rather than make targets, so
`make e2e-governed-union` (a target that does not exist) sails past it.

**Fix:** either write the missing targets and give the security-shaped ones (`governed_union`,
`gateway`, registry CAS) a CI lane, or delete the 12 declarations and the docstrings citing them.
Then extend the collection gate: every declared marker must match ≥1 test AND be named by ≥1
invocation site; every `make <target>` cited in a suite docstring must exist.

### H2 — `tests/e2e-py/test_auth_e2e.py:91` · asserts a 403 the catalog stopped issuing

```python
# Valid token, no tuple → 403 (OpenFGA denies).
assert requests.post(f"{server}/v1/namespace/e2ens/create", ...).status_code == 403
```

Today `_create_parent_check` returns `None` for a top-level namespace unless `fga_lock_root_create`
is set — and it is set nowhere in the stack this suite documents. The create is **allowed**. The
follow-on `_grant(..., "writer", "namespace:e2ens")` is inert too: the create gates on the PARENT,
never on the object being created.

The repo contains both answers. `scripts/auth_e2e.sh` — the script CI actually runs — says
`expect 200 ... "alice create namespace"`. Two artifacts assert opposite outcomes for the same
request; only the skipping one claims the deny.

**Fix:** decide which is the intended posture. Either assert the open-create default plus a NESTED
create (which does gate), or run the stack with `LANCE_FGA_LOCK_ROOT_CREATE=true` — the configuration
the assertion actually describes. Then give the file the CI lane `e2e-auth` already boots.

### H3 — `nav-truth.test.ts:104-110` · the scanner drops 5 of 45 leaves; the floor absorbs it — **CLOSED**

One bounded regex pairs `title:` with `href:`. Measured against a raw `href:` count per file:

```
compute     scanned 10   declared 12   <-- MISMATCH
explorer    scanned  5   declared  6   <-- MISMATCH
lakehouse   scanned 12   declared 14   <-- MISMATCH
TOTAL       scanned 40   declared 45      (floor is > 30)
```

Never asserted by the gate: **`/`, `/settings`** (the estate navbar, rendered in all seven zones) and
`/compute/gpu`. Worse, the pairing SHIFTS — compute's first row glues the ZoneNav's own label
`'Compute'` to the `Overview` leaf's href, so a failure would name the wrong leaf.

The file's own comment records this exact regression happening once before ("the window was 200 chars
and that SILENTLY dropped leaves") and "fixing" it by widening to 900. That treated the symptom. The
defect is using a bounded-window regex at all.

**Fix:** parse per object literal, then assert the pair count equals the raw `href:` count in that
file — a self-consistency check, not a magic floor. Raise the floors to the real counts.

**CLOSED.** Fixed as written, and the audit UNDERSTATED it: 5 was the shell's miss alone — the zone
sidebars miss 5 more, so **10 of the estate's 90 nav hrefs were asserted by nothing**
(`/compute/gpu`, `/compute/serve`, `/lakehouse/lineage/columns`, `/lakehouse/workbench`,
`/explorer/graph`, and the shell's `/explorer/workflow`, `/models/runs`, `/lakehouse/admin/dlq`, `/`
and `/settings`). Root cause measured, not guessed: the trailing `(?=title:)` lookahead had to be
reached within the window, so a leaf whose gap to the NEXT leaf exceeded 900 chars was dropped whole
— compute's `Actors` match ended at L38 and the next began at L54, taking `GPU` with it. The regex is
replaced by a brace-depth frame walk that consumes comments and string bodies (both load-bearing:
`models/nav.ts` documents this regex in prose containing "href:", and `nav-config.ts` declares
TypeScript `href: string;` members inside `{…}` type literals).

Verified both directions. Forward: pointing `/compute/gpu` at a missing route reds naming `"GPU"`,
while the old regex ships that same break GREEN. Backward: putting the old regex back under the new
self-consistency assertion reds all four files, naming all ten hrefs — matching an independent count
exactly. The phantom leaf is gone too: the gate reported `compute: "Compute" -> /compute` (the
ZoneNav's own label glued to the Overview leaf's href) and now reports `compute: "Overview"`.
105 tests pass, up from 89.

### H4 — `models/package.json` · declares `test:e2e` at HEAD with zero test files

`git ls-tree -r HEAD frontend/microfrontends/models/` → 32 files, **no `e2e/`, no
`playwright.config.ts`** — while `package.json`, which IS committed, declares
`"test:e2e": "playwright test"`. The harness exists on this machine, untracked.

I verified this by hand before the audit ran, and the challenger confirmed it and **found it is now
worse**: models is the fail-fast task that suppresses the other four zones' e2e.

**Fix:** commit the harness in the same commit as any ci.yml edit, and verify with
`git show HEAD:<path>` rather than a green local run — the local run passes precisely because the
untracked files are on disk. *(Not mine to commit — flagged for whoever owns them.)*

---

## MEDIUM

| # | where | what |
| --- | --- | --- |
| M1 | `Makefile:45-50` | **`make test-slow` cannot run one slow test.** Leg 1 selects zero; leg 2 exits 4 at collection because it omits the `cd` that `make test` documents as mandatory. Fails on the *second* line after a 2-minute green suite, so it reads as "no GPU on this box". |
| M2 | `.dagger/test.go:86-139` | **No CI job runs any `runners/*` suite** — 58–60 tests guarded only by a developer typing `make test`. The job is titled "Runs the whole offline suite"; it covers 4314 tests and never mentions the runners. |
| M3 | `tests/e2e/tests/mfe.spec.ts` | The estate's only deployed-fleet browser smoke still hydrates two DELETED zones — and is invoked by nothing (`tests/e2e` is outside the frontend workspace, so turbo cannot discover it). The challenger found it **vacuous, not merely stale**: the login-first gate makes every route — deleted, live or invented — assert 200 from the Dex page. |
| M4 | `transport-contract.test.ts:241` | The cross-zone schema gate **never compares `createWarehouse`** — the case the file was written for. `upstreamPath` matches a backtick-slash-backtick inside a prose comment and splits the key. Both guards written to prevent this are satisfied by the split halves. |
| M5 | `test_gateway_e2e.py:38` | Probes the RETIRED nginx URL space and a text health body — 2 of 3 tests would fail on a live gateway. Docstring cites `make e2e-gateway`, which does not exist. |
| M6 | `test_worker_queue.py:107` | The poison-park test never provisions the DLQ stream its own subject needs. Passes on borrowed state (the chart's `nats-stream-job` created it) that it never asks for and never asserts. Reproduced failing on a bare NATS container. |

---

## LOW

- **`explorer/package.json:16`** — `test:e2e:live` (3 files, ~26 KB) is invoked by nothing and exits 2
  before its first assertion. Deletion cost: it holds the estate's only explicit WebGPU-adapter
  assertion.
- **`dev-zone.test.ts:62-73`** — the "guards the scanner itself" test counts zone DIRECTORIES, not
  scanner matches. If the configs hoisted their env into a shared helper, every zone would `continue`
  and the file would collapse to one passing test — taking the currently-red annotator assertion
  green *by disappearance*.
- **`test_e2e_collection_gate.py:54`** — `MIN_SUITE_FILES = 24` against 25 real suites. One suite can
  stop collecting and the gate stays green, which is the exact failure it was written to prevent, at
  a scale of one.
- **`no-networkidle.test.ts:37`** — 2 of 7 bans scan a directory that does not exist; a missing dir
  yields `[]`, indistinguishable from clean. The repo's one live violator sits outside the scan root.

---

## REFUTED — do not act on these

The adversarial pass killed four. Recording them so nobody re-files them:

1. **`ci.yml:282` "web-e2e runs at most ONE zone"** — refuted by counter-samples inside the auditor's
   own time window. `--concurrency=1` is not fail-fast in the way claimed.
2. **`test_fga_model_contract.py:253` "asserts a hand-written dict"** — the dict is only the LEFT
   operand; the right is `load_model()` reading the real model. It is a live cross-check, and the
   "unguarded" sites are gated by the `ms-authz` CI job.
3. **`test_invariants.py:1730` "Ray-address check unreachable"** — mechanism confirmed (the branch IS
   dead across five chart renders), conclusion refuted.
4. **`dev-zone.test.ts:73` "emits no assertion"** — the mechanical observation is right, but the
   assertion **fires and is RED at HEAD**; every conclusion drawn from it fails on execution.

---

## What I would do first

**H1 and H2 are the same defect in two places: a security-shaped test that no lane runs.** The
governed-union, gateway and registry-CAS suites are exactly the ones an estate wants guarded, and
they are guarded by nothing. Wiring one CI lane closes more real exposure than fixing all four LOWs.

**H3 is the cheapest high-value fix** — one parser change plus raising two floors, entirely inside a
test file, no production behaviour touched.

**H4 needs its owner**, not me: the files are untracked and belong to whoever created them.

Nothing here is urgent in the sense that production is broken. It is urgent in the sense that the
suite's green is worth less than it looks, and the gap is widest exactly where the estate has decided
correctness matters most.
