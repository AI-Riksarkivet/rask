# Handover — moving rask to another machine

Written 2026-09-23. Everything below was measured on the current host, not assumed.

## 0. Read this first if the new machine is the DGX Spark

**The estate will not come up on arm64 today.** `apache/age:release_PG16_1.5.0` is `linux/amd64`
only — read from the image's config blob — and it is the `rask-age` StatefulSet, which serves
**both** the lineage graph **and** OpenFGA. On arm64 nothing governed starts at all.

Two smaller arm64 snags beside it: `.dagger/charts.go:37,66` curl `helm-…-linux-amd64.tar.gz` and
`prometheus-…linux-amd64.tar.gz` into the chart-lint container.

**MEASURED 2026-09-23 AGAINST THE REGISTRY, AND IT IS SMALLER THAN THE ROW SAYS.** The blocker is
the PIN, not the image. Queried on Docker Hub:

| tag | platforms |
| --- | --- |
| `release_PG16_1.5.0` (pinned today) | amd64 only |
| **`release_PG16_1.6.0`** | **amd64, arm64** |
| `release_PG17_1.7.0`, `release_PG18_1.8.0` | amd64, arm64 |

`1.6.0` is the next patch of the SAME PostgreSQL major, so the move is a version bump rather than an
AGE build, a distribution swap, or the CNPG cutover [[XC-070]] proposes. The pin lives in four places:
`chart/values.yaml:3050`, `chart/values-live-pins.yaml:5`, `.dagger/e2e.go:11`, and the comment at
`chart/templates/age-postgres.yaml:3`.

**DONE AND PROVEN ON THE LIVE ESTATE 2026-09-23 — you do not need to do this on the new machine.**
The pin is `release_PG16_1.6.0` in all four places. It was rolled onto this host's `rask-age`
StatefulSet, which holds real lineage and FGA data, and everything answered:

* `rask-age-0` Running, **0 restarts**, image `apache/age:release_PG16_1.6.0`;
* the graph answers through psql (`SELECT count(*) FROM ag_graph`);
* OpenFGA `/stores` 200 and a real `check` verdict;
* **the lineage service reads the graph end to end — `GET /datasets` 200, `total: 433`.**

**No `ALTER EXTENSION age UPDATE` was needed**: the 1.6.0 library runs against the 1.5.0-installed
extension. Worth knowing because that was the risk this bump was being held back for.

*One artefact of upgrading IN PLACE, which a fresh estate will NOT see:* postgres warns `database
"lineage" has a collation version mismatch ... created using collation version 2.36, but the
operating system provides version 2.41`. That is glibc moving between the two image bases. On the new
machine the databases are created by the 1.6.0 image itself, so the versions agree and the warning
never appears. If you ever hit it on an upgraded host, the remedy is `REINDEX DATABASE` then
`ALTER DATABASE <db> REFRESH COLLATION VERSION`.

The other half of XC-070 is already fixed: `.dagger/charts.go` reads the arch off the container with
`dpkg --print-architecture` for both the helm and promtool fetches. The row's "one tooling pin the row
missed" is stale.

**The code and the frontend are fine on arm64.**

If the new machine is x86_64, ignore this section.

## 1. What travels by itself

`git pull` and you have it: all source, the Helm chart, every dockerfile, `open_backlog_left_new.md`,
`docs/DECISIONS.md`, `.claude/settings.json` (team-shared), the vendored `.claude/skills/rask-*`.
Working tree is clean and `origin/main` is current.

The doc **"Lakehouse — what is actually left"** lives in the cloud, not the repo:
<https://claude.ai/code/artifact/59c11ca2-fea7-4c91-acb0-6c0be6d5818a>

## 2. Files you must COPY BY HAND (gitignored, not in git)

| File | Why it matters |
| --- | --- |
| `.env` | 28 lines: `HCP_*` credentials, `AWS_REGION`, bucket and IIIF endpoints. **Nothing else has these.** |
| `.claude/settings.local.json` | Your `autoMode`, permissions, and the **Stop hook** that injects the FOCUS block every turn. Without it the focus discipline is gone. |
| `~/.claude/projects/-home-gabriel-Desktop-rask/memory/` | 38 memory files, 164 KB — the accumulated traps (kubeconfig, ack_floor, prefix deletes, …). Copy to the same path on the new host, adjusting the directory name if the repo path changes. |

Everything else gitignored (`.venv/`, `node_modules/`, `.localbin/`, `.svelte-kit/`, `build/`,
`chart/charts/`, `.dagger/internal/`) is regenerated — do not copy it.

**That is the whole list: three things by hand, everything else `git pull`.** The FOCUS block's TEXT
travels in git (it lives in `open_backlog_left.md`, which is tracked); what does not travel is the
Stop hook that injects it, which is why `.claude/settings.local.json` is on the list above.

## 3. Install on the new machine

Must be on PATH before anything else: **git, docker** (the daemon only — Dagger drives it; you still
never run `docker build`), **helm**, **uv**, **bun**, **prek**, and the **claude** CLI.

```bash
make install           # bun install + uv sync + prek git hooks (BOTH hook types)
make bootstrap         # kind/kubectl/fga/k9s into .localbin
make claude-bootstrap  # svelte MCP + the ra-skills marketplace + plugins
```

`make install` is what wires the commit-msg hook — without it the Conventional-Commit and
no-Co-Authored-By gates are silently dead.

## 4. Bringing the cluster back

**None of the cluster travels.** This host runs k3s at helm revision **222**, 38 deployments,
`KUBECONFIG=/etc/rancher/k3s/k3s.yaml`. On the new machine it is a fresh build:

```bash
make k3s-install    # once — k3s + helm
make dagger-engine  # once per host, and after a Dagger CLI upgrade — the engine every build goes through
make k3s-build      # every first-party image at :$(K3S_TAG) (default `dev`), via Dagger
make k3s-import     # side-load them into k3s containerd (sudo — run it in a real terminal)
make k3s-up         # install/upgrade the release into namespace `rask`
```

Verified end to end on the DGX Spark (arm64) on 2026-09-23. On a fresh host `make k3s-up` needs nothing
installed by hand:

* **The External Secrets Operator ships in the chart** for a local estate (`externalSecrets.operator` on
  in `values-local.yaml`; off by default and in `values-prod.yaml`, where the cluster's own operator
  serves). Its CRDs are vendored in `chart/crds-bootstrap/` and applied by `make k3s-crds`.
* **The release lives in namespace `rask`** (`NAMESPACE ?= rask` in the Makefile). In k9s: `:ns rask`.
* **Images match what the build made**: one `K3S_TAG` for build, import and install, and
  `chart/values-live-pins.yaml` is applied only to the cluster it was captured on (it records the
  `kube-system` UID).
* **A local admin exists**: sign in as `alice@example.com` / `password`; `values-local.yaml` makes that
  dex user the bootstrap admin.
* **Starting over is `make k3s-purge && make k3s-up`**: the purge deletes every volume in `rask` (the
  Dapr scheduler's included), pre-delete hooks release the Kueue queues and the ExternalSecrets while
  their controllers still run, and a pre-install hook sweeps any bootstrap Job an earlier install left
  behind, so the next install runs its own.

`make dev-registry` is only needed to PUSH images to a registry instead of side-loading them. When you
use it, the registry is addressed as `localhost:5000` by k3s and `172.17.0.1:5000` by Dagger (same
container, different vantage point), and Dagger always speaks HTTPS with no `--insecure`, which is why
`make dagger-engine` writes the engine's own config.

The estate's data does not travel either — the Lance datasets, the FGA store and the lineage graph are
all in-cluster. A new estate starts empty, which is fine: it is all test data.

## 5. Where the work stands

**202 open, 100 blocked on a decision, 102 workable.** Lakehouse phase 1: 35 open, 3 workable.

Shipped and proven live on 2026-09-23 (13 commits): LH-192's absent-dataset detector, LH-164's
registration unwind and `unreadable_locations` coverage field, LH-183's pass-budget retirement plus
the drain and index-lane fixes, LH-191's planner-side cadence skip.

**Three decisions are the critical path**, all with live numbers in the doc:
1. May the sweep reclaim a branch? — 120 datasets, 21% of everything the sweep walks.
2. The per-tier compaction cadence numbers — the planner is ready and reports correctly; all 27
   policies still carry `compact_interval_hours: null`.
3. [[LH-194]] — how a failed seed's registration is removed, given a machine may not deregister what
   it registers.

**Both arms of the cadence skip are proven live**, so nothing is owed there: `compact_enabled: false`
gave `policy_disabled: 1`, and `compact_interval_hours: 8760` (stamped by driving the unit once, with
the policy left in place) gave `planned=569 published=569 skipped_by={'trashed': 14,
'policy_interval': 1}`.
