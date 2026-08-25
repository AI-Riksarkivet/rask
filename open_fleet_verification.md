# open: fleet verification after the stale-image rebuild

**Why this exists.** Every "it works" claim before 2026-08-25 was measured against images
**349–728 commits stale**. Rebuilding the fleet onto HEAD broke seven things. This tracks what was
found, what is fixed, and what is genuinely left — so "done" means measured, not asserted.

Delete this file when the Left section is empty.

---

## Estate as of now

| | |
| --- | --- |
| Fleet images | all seven on `main-2b6e4978` (gateway, compute, controlplane, ingest, notifications, flows, lance-rest-catalog) |
| Unhealthy pods | 0 |
| Namespaces | project-qualified `lakehouse-{bronze,silver,gold}` under `warehouse:lakehouse-wh`; estate-level `bronze-media`/`silver-media` |
| Ray | one head (`ray-lance-head`) — the orphaned RayService and its two idle clusters are deleted |
| Promotion review | **ON** (`MEDALLION_QUALITY_REVIEW_ENABLED=true`, band 0.25, approver = alice) |

## Live suite results

| suite | result |
| --- | --- |
| medallion | 5 passed |
| media | 1 passed |
| gateway | 3 passed |
| dummy_lane | 3 passed, 4 skipped |
| cas | 5 passed |
| compaction | 10 passed, 1 skipped |
| governed_union | 2 passed, **2 failed** — promotion-review holds |
| auth | **1 failed** — not yet diagnosed |
| observability | not yet run cleanly |

---

## Done

Each of these was found by rebuilding, and each is fixed and pushed.

1. **`ray[serve]` missing from the head image** — the KubeRay operator polls the head dashboard's
   `GetServeDetails`; without it that answers 501, a zero-downtime upgrade can never confirm the
   pending cluster is healthy, and the estate keeps BOTH RayClusters forever
   (`BothActivePendingClustersExist`, stuck two days). `d4f41aca`
2. **`jinja2` missing from `ray[serve]` 2.58.0** — upstream declares it in no extra while
   `ray/serve/_private/haproxy.py:19` imports it at module load. `66ca6f5f`
3. **The per-workload Ray image could not be built** — `FROM ray-cluster:dev` is a host-daemon tag and
   BuildKit resolves against a registry; replaced by a parametrized `ray-runner.dockerfile`. `971c6156`
4. **Medallion tiers were flat at the root** where no warehouse could own them, then **nested**, which
   collided with the project qualifier and produced `lakehouse-lakehouse$gold$catalog` — an id nothing
   can create. Now bare names, qualified at runtime. `44c4f8fc`, `2b6e4978`
5. **Half-renamed producer/mover pairs** — a lane mismatch makes the mover return DROP, which acks as
   SUCCESS: 200 OK on every hop, no error in the mover's log. `09594be7`
6. **The namespace seeder named a bucket where a warehouse id belongs** — three different objects are
   called `lance-catalog` and none of them is a warehouse. `7f685f29`
7. **Four e2e suites encoded contracts the fleet had moved past** — project-admin bearer for a
   project-scoped produce, lineage reads as the project admin, media is not project-qualified,
   gateway `/healthz` is JSON, `body.lane` → `body.name`. `feec9563`
8. **CAS suite could not spawn workers** — `--import-mode=importlib` names a suite
   `tests.e2e-py.test_…`, which nothing can import; worker extracted to a flat sibling module.

---

## Left

### 1. Promotion review blocks three cascade suites — DOING NOW

`medallion`, `media` and `governed_union` all wait for gold. This estate holds every **first**
promotion for human approval (`first_promotion` trips the 0.25 band, since a first promotion has no
predecessor to compare against). The suites wait, the hold waits for a human, the suites time out.

Not a defect — a test-vs-policy mismatch. Doing both halves:

- **A.** A drive-level switch so a cascade suite can run against an estate with review off.
- **B.** The suites approve a hold when they meet one, so they pass on *any* estate and exercise the
  governance loop rather than sidestepping it.

### 2. `auth` suite — 1 failed, undiagnosed

Ran for the first time today (needed `LANCE_E2E_AUTH_SERVER`). Not yet looked at.

### 3. `observability` suite — never produced a result

Needs `LANCE_E2E_GREPTIME_URL`; wired the port-forward but the run returned nothing. Re-run and read.

### 4. Ray is owned by no Helm release

The cascade runs on `ray-lance-head`, a hand-applied demo deployment. The chart only renders its
RayService under `singleTenant.enabled`, which is off — which is how it became an orphan. Fine for now
by owner call, but a chart-rebuilt estate returns to the pre-fix topology.

### 5. Orphaned legacy data

`bronze` still holds a `pages` table at `s3://lance-catalog/medallion/bronze`. The cascade now writes
to `lakehouse-bronze`. Nothing reads the old one. Migrate or drop — not deleted unasked.

### 6. `POST /ingest-media` names a modality on a platform door

Raised by the owner 2026-08-25, and it is a real inconsistency rather than a preference.

The name cannot simply become `/ingest`: `services/ingest` already owns that word — its own service on
`:8830` with `POST /ingests`, reached through the gateway at `/api/ingest/*`, and it is the actual
pre-bronze acquisition plane (enumerate, validate, sha256, land). A bare `/ingest` on the medallion
producer would collide with it.

But `-media` is a MODALITY, and CLAUDE.md forbids exactly that: "a data type must never enter a shared
seam" (:230), and the very sentence that blesses this door also says "there is no protocol-specific
ingest door, and adding one would make that protocol privileged" (:194).

The implementation is generic — bytes in, bronze blobs out, trigger a chain; nothing in it decodes an
image or reads a MIME type. What is modality-specific is only the WIRING (`mediaBronzeNamespace`, the
`medallion.media` topic), which is config. So it is a lane-scoped door whose lane is called "media",
and the lane name leaked onto the platform's public surface.

A neutral name (`/ingest-blobs`, `/ingest-objects`) would match both the implementation and the rule.
Blast radius: the chart, the e2e suites, the docs, and CLAUDE.md's own three-doors sentence. Not done
mid-flight; it wants its own change.

### 7. kueue-setup job crash-loops

TLS cert rotation: `certificate signed by unknown authority "kueue-ca"` on the conversion webhook.
Pre-existing infra, unrelated to the rebuild, but it blocks `helm --wait`.
