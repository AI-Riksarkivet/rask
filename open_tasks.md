# open_tasks — the pinned engineering task list

**What this is.** The OPEN backend/platform tasks, in one place, because the live list (`/tasks`) dies
with the session. **Not** `TODO.md` — that is the product/frontend backlog (26 items: routes, sidebar,
Explorer, annotate, studio).

**This is an INDEX, not a copy.** Every item points at the document that owns it. A second full
statement of a task drifts from the first, and then nobody knows which one is true. Read the source
before starting; update the source, not this file, when the work moves.

**Closed items are not listed. Git history is the record of those** — and that rule is load-bearing,
not tidiness. This file is itself an `open_*.md`: ephemeral by design, deleted when the queue empties.
Writing history into it means writing history into something scheduled for deletion. Nothing here may
be the only copy of anything.

**Nothing may cite this file as an ADDRESS.** Durable code — Python, chart templates, YAML — cites no
`open_*.md` at all. A sibling plan may restate a claim from another inline; it may not link to one.
`open_dapr.md` accumulated 74 such citations across 45 files before it was deleted 2026-08-15, and
every one had to be rewritten first.

**Scope.** The wider estate queue — frontend (#110, #111, #116, #130, #147), owner rulings (#98, #134,
#143, #146), and the lakehouse/catalog items (#43, #48, #56, #67, #84, #85, #91, #142) — lives in the
session task list and in `open_lakehouse_diff2.md` §5. `diff2` §5 row 2 (F2, time-boxed grants) is
**half done**: the wrapper half landed 2026-08-14 (`b58eff4f` — `condition_context()` + `context` on
all four read wrappers); the catalog call-site half (`_require` passes no context) is still open.

---

## BLOCKER — the release can no longer be upgraded AT ALL. Yours to decide.

`helm upgrade` now fails before it applies anything:

```
Error: UPGRADE FAILED: update: failed to update: Secret "sh.helm.release.v1.rask.v35"
is invalid: data: Too long: may not be more than 1048576 bytes
```

This is not about the manifest. Helm embeds the WHOLE CHART in every revision, and ~880 KB of
`chart/charts/*.tgz` is ALREADY-COMPRESSED subchart archives that gzip cannot shrink further.
Measured 2026-08-15:

| part | size |
| --- | --- |
| vendored subchart archives (10 of them) | ~880 KB, incompressible |
| rendered manifest, gzipped | 262 KB |
| **release secret, v34 / v35** | **1,046.9 KB / 1,048.5 KB** vs a 1,024 KB hard limit |

It has been creeping up for months — v28 was 964 KB — and v35 crossed the line. Every revision
since v28 was one small edit away from this. It blocks `make k3s-up` and CI, not just this session.

**Do not "fix" it by trimming comments** (worth only ~39 KB gzipped) **or by disabling Kueue.**
Kueue is idle today — 0 workloads, and the only `queue-name` reference in the repo is a comment in
`values.yaml` — so `kueue.enabled=false` would buy 184 KB with no functional loss TODAY. It is still
deleting an operator that was installed on purpose, to dodge a storage limit, at the wrong layer.

**Four workarounds were measured and all four are dead**, so nobody re-tries them:

| attempt | result |
| --- | --- |
| trim rendered comments to Helm template comments | broke the render TWICE — ate content inside block scalars (`- \|`, ConfigMap SQL). Reverted both times |
| drop an unused subchart | all ten are genuinely enabled |
| unpack `charts/*.tgz` so gzip can compress them | measured: packed 871.9 KB, unpacked 882.8 KB — **costs 11 KB** |
| `kueue.enabled=false` | buys 184 KB, and Kueue is provably idle (0 workloads, the only `queue-name` reference in the repo is a comment) — but it deletes an operator installed on purpose, to dodge a storage limit |

The two real fixes, both yours:

1. **Split the chart** — infra (operators + CRDs, installed rarely) from app (upgraded constantly).
   This matches how the estate actually operates and is the architecturally correct answer.
2. **Move Helm to the SQL storage driver** (`HELM_DRIVER=sql`) against the Postgres already running.
   No 1 MiB limit. Smaller change, keeps one chart.

**✅ OPTION 2 IS DONE — the release is `deployed` again (2026-08-15).** `helm history rask` under the
SQL driver shows revision 1, `Install complete`, and every image survived on its intended tag. Both
hook jobs that failed revision 34 passed (`72505fef` NATS retention, `5d93e6f3` AGE extension).

**THE ENV VARS ARE NOW LOAD-BEARING.** Every `helm`, `make k3s-up` and CI invocation must carry them,
or Helm reads the empty Secret backend and concludes nothing is installed:

```bash
AGEIP=$(kubectl get pod rask-age-0 -o jsonpath='{.status.podIP}')   # ClusterIP is not host-routable; the pod IP is
export HELM_DRIVER=sql
export HELM_DRIVER_SQL_CONNECTION_STRING="postgresql://lance@${AGEIP}:5432/helm?sslmode=disable"
```

The password lives in `~/.pgpass` (mode 600), not the DSN — lib/pq reads it, and a credential does not
belong on a command line. **The pod IP changes when the AGE pod restarts**, so re-derive it; a stable
answer wants a Service DNS name reachable from wherever helm runs, which is one reason splitting the
chart (option 1) is still the better end state.

**READ THE VALUES WITH THE SECRET DRIVER, UPGRADE WITH THE SQL ONE.** Exporting `HELM_DRIVER=sql`
before `helm get values rask` returns ZERO values, because the old release lives in the Secret backend.
The chart then refused with *"image.repository must be set … A bare name resolves to Docker Hub and
will ImagePullBackOff"* — the #135 guard doing exactly its job, and the reason nothing was applied.
Scope the SQL vars to the `helm upgrade` line alone.

<details><summary>The command that did it</summary>

```bash
helm get values rask > /tmp/lv.yaml            # SECRET driver — no HELM_DRIVER set
AGEIP=$(kubectl get pod rask-age-0 -o jsonpath='{.status.podIP}')
HELM_DRIVER=sql HELM_DRIVER_SQL_CONNECTION_STRING="postgresql://lance@${AGEIP}:5432/helm?sslmode=disable" \
  helm upgrade --install rask ./chart --take-ownership --wait --wait-for-jobs --timeout 9m \
  -f /tmp/lv.yaml -f chart/values-live-pins.yaml
```
</details>

Historical, kept because the reasoning still applies: the `helm`
database exists in the AGE Postgres with `search_path=public` and the AGE extension (without the
extension every `DROP` there fails — see the fix above), a `CREATE TABLE`/`DROP TABLE` round-trip
passes in it, the pod network is routable from the host so no port-forward is needed, and
`helm list -a` connects through the driver and returns an empty release list. Only the write is
left, and it was refused by this session's permission classifier four times (it carries a DSN):

```bash
cd /home/blackwell/Desktop/rask && export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
helm get values rask > /tmp/lv.yaml
AGEIP=$(kubectl get pod rask-age-0 -o jsonpath='{.status.podIP}')
HELM_DRIVER=sql HELM_DRIVER_SQL_CONNECTION_STRING="postgresql://lance:lance@$AGEIP:5432/helm?sslmode=disable" \
  helm upgrade --install rask ./chart --take-ownership --wait --wait-for-jobs --timeout 9m \
  -f /tmp/lv.yaml -f chart/values-live-pins.yaml
```

It reads the CURRENT values out of the Secret-based release, so nothing is invented, and
`--take-ownership` adopts the running resources (Helm has no history under the new driver, so this
is an install that adopts, not an upgrade). Fully reversible: the Secret-based release records are
untouched — drop the two `HELM_DRIVER*` vars to return to exactly today's state.

**The standing cost, and why option 1 is still the better answer:** every later `helm`, `make k3s-up`
and CI invocation must carry those two env vars, or Helm reads the empty Secret backend and concludes
nothing is installed.

Both hook jobs that failed revision 34 are fixed (`72505fef` NATS retention, `5d93e6f3` AGE
extension), so they should pass on the next run.

Interim state, verified: revision 35 was left `pending-upgrade`, which refuses every later upgrade;
its release secret was deleted so 34 is latest again. **Workloads are correct and healthy regardless**
— every service is on its intended image and 0 pods are outside Running/Completed. What is stale is
the RELEASE RECORD, not the estate.

---

## The chart changes still waiting on that window

Chart changes are committed and render correctly but are NOT in the release. `make k3s-up` owns it —
a hand `helm upgrade` with different values replaces every deployed image with the chart default.

- `lance-statestore` now scoped to the mover app-ids. **daprd cannot hot-reload an actor state
  store**, so a mover pod that started before this keeps the OLD scope list and fails to dispatch on
  every delivery. The medallion deployments need a restart. The workflow is new, so no in-flight
  instances exist and **no drain is required**.
- `medallion.compute` / `medallion.ray` now default true, with `rayAddress` derived when empty. This
  supersedes the env I set by hand on `rask-bronze-to-silver` while driving S1 — the upgrade replaces
  improvised state with declared state.
- `MAINTENANCE_ORPHAN_SCAN_ENABLED` — the running maintenance pod carries no such variable at all.
- The observability retention floor, and `dapr.io/config` gated on `dapr.enabled`.
- The otel-collector app-log filter (applied by hand 2026-08-15; the upgrade makes it durable).

---

## Medallion — owned by `open_medallion_workflow.md`, not restated here

Two items were listed here as open and were WRONG, because they were second statements of things that
doc already owns and had already settled. This is what the index rule is for; read the owner first.

- The promotion review band is **DECIDED** (±25%, plus first-promotion-of-a-dataset) — §9 item 1.
- The workflow management surface is **DESIGNED**, not an open question:
  `POST /api/medallion/promotions/{id}/decision`, FGA-gated on `can_promote`, mounted inside
  `RASK_API_PREFIX`. Unbuilt because it is S3.

That doc also owns the constraint that an activity payload must fit **4 MiB** — the workflow worker's
own gRPC channel, not daprd's `--max-body-size`. Deriving that number independently instead of reading
it there shipped a flows bound at 4x the real ceiling (`f95be037` corrects `0bbcc035`).
