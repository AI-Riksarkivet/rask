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

## 1. CI still calls helm without the driver. (The Makefile half landed in `ba6b1f91`.)

**Done:** `scripts/helm.sh` is now the one seam and `$(HELM)` points at it, so `k3s-up`, `k3s-down`
and `kind-deploy` are correct. It derives the AGE pod IP per call, keeps the password in `~/.pgpass`
rather than the DSN, refuses to run when it cannot resolve the address, and passes read-only
subcommands straight through so `make k3s-install` still works with no cluster.

**Left:** `.github/workflows/ci.yml` references helm and needs the same treatment — route it through
`./scripts/helm.sh`, or give the job the two env vars. It was another session's in-flight file when
this landed, so it was deliberately not touched. Everything below still applies to it.

The release moved to Helm's SQL storage driver on 2026-08-15 (the Secret backend hit Kubernetes' hard
1 MiB limit — see item 2). Every `helm` invocation must now carry:

```bash
AGEIP=$(kubectl get pod rask-age-0 -o jsonpath='{.status.podIP}')   # ClusterIP is NOT host-routable
export HELM_DRIVER=sql
export HELM_DRIVER_SQL_CONNECTION_STRING="postgresql://lance@${AGEIP}:5432/helm?sslmode=disable"
```

The password lives in `~/.pgpass` (mode 600), never the DSN. **Without these vars Helm reads the empty
Secret backend, reports the release as absent, and `--install` re-installs over a live estate** — the
worst kind of failure, because it looks like a fresh cluster rather than an error. `Makefile::k3s-up`
sets neither, and neither does CI.

Two traps to encode wherever this is fixed, both hit for real:

- **Read values with the SECRET driver, upgrade with the SQL one.** `helm get values rask` under
  `HELM_DRIVER=sql` returns ZERO values, because the old release lives in the Secret backend. The
  chart then refuses (`image.repository must be set … will ImagePullBackOff`) — the #135 guard doing
  its job. Scope the SQL vars to the `helm upgrade` line alone.
- **The AGE pod IP changes on restart.** A stable answer needs a Service DNS name reachable from
  wherever helm runs, which is really an argument for item 2.

## 2. The 1 MiB release ceiling is still there. The SQL driver only routed around it.

Helm embeds the whole chart in every revision, and ~880 KB of `chart/charts/*.tgz` is
already-compressed archives gzip cannot shrink. Measured: v28 964 KB → v35 1,048.5 KB against a
1,024 KB limit. **Four workarounds were measured and all four are dead** — do not re-try them:

| attempt | result |
| --- | --- |
| convert rendered YAML comments to Helm template comments | broke the render TWICE (ate content inside block scalars — `- \|`, ConfigMap SQL). Reverted both times |
| drop an unused subchart | all ten are genuinely enabled |
| unpack `charts/*.tgz` so gzip can compress them | packed 871.9 KB vs unpacked 882.8 KB — **costs 11 KB** |
| `kueue.enabled=false` | buys 184 KB, and Kueue is provably idle (0 workloads; the only `queue-name` reference in the repo is a comment) — but it deletes an operator installed on purpose |

**The real fix is splitting the chart**: infra (operators + CRDs, installed rarely) from app (upgraded
constantly). That also dissolves item 1, since the app chart would fit the Secret backend again.

## 3. Dapr — one design item and one decision. Everything else is closed.

The workstream is otherwise done: determinism review clean across all three workflow bodies, the
activity and management rules satisfied, `open_dapr.md` deleted with its 74 citations.

- **`stage_run` is a Monitor without `continue_as_new`.** Dapr's docs are explicit that an unbounded
  poll loop is an anti-pattern and `continue_as_new` is the API. `MAX_POLLS = 2880` at 30 s = 24 h,
  ≈5,760 history events in ONE instance. Bounded, so not the literal anti-pattern; the code names this
  and defers it to S2. Changing a live workflow's action sequence is deploy-coupled — read
  `tests/unit/test_workflow_action_order.py`'s docstring before touching it.
- **Outbox: DIY vs native — CLOSED (2026-08-15), see `docs/DECISIONS.md`.** The application-side
  outbox stays. The proposed escape — write a transactional marker to Dapr state after the Lance write
  and let the native outbox guarantee the publish — does NOT work: a marker written after the commit
  leaves the identical crash window, relocating the gap rather than closing it. Dapr's outbox publishes
  inside a transaction Dapr itself runs, which requires the write to go through its state API, and
  rask's authoritative writes (Lance via the Lance library, `lineage_events`/AGE via psycopg) do not.
  The bound underneath: atomicity between object storage and a broker does not exist anywhere, so the
  goal is no silent loss, not atomicity.

## 4. ✅ CLOSED — the cascade's success path is witnessed (2026-08-15 20:59).

bronze -> silver -> gold, all three COMPLETE in the lineage graph, driven through `POST /produce`.
Gold completing is also the proof silver was readable and non-empty, since gold reads it.

It had never run because of TWO stacked image faults, not missing data (`ebb46650`, `faa3e701`):
the job scripts were not baked into the cluster image (`exit 2`), and once they were, that image is
the HTR/CUDA one and has no pylance (`exit 1`). `medallion.ray` is OFF again until a Lance-capable
cluster exists — do not re-enable it against the HTR image.

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
