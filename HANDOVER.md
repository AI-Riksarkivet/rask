# Handover — moving rask to another machine

Written 2026-09-23. Everything below was measured on the current host, not assumed.

## 0. Read this first if the new machine is the DGX Spark

**The estate will not come up on arm64 today.** `apache/age:release_PG16_1.5.0` is `linux/amd64`
only — read from the image's config blob — and it is the `rask-age` StatefulSet, which serves
**both** the lineage graph **and** OpenFGA. On arm64 nothing governed starts at all.

Two smaller arm64 snags beside it: `.dagger/charts.go:37,66` curl `helm-…-linux-amd64.tar.gz` and
`prometheus-…linux-amd64.tar.gz` into the chart-lint container.

This is [[XC-070]] in `open_backlog_left_new.md`, with the options written out (an arm64 AGE build, a
different AGE distribution, or the CNPG-with-extension path — `.docker/cnpg-age-ext.dockerfile`
exists but needs K8s 1.33+ and CNPG >= 1.27). **The code and the frontend are fine on arm64; it is
one image and two curl strings.**

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
make k3s-install    # once
make dev-registry   # once per host — registry on :5000, points k3s at it (sudo, restarts k3s)
make dagger-engine  # once per host — the engine allowed to push to a plain-HTTP registry
make k3s-build && make k3s-import && make k3s-up
```

Two traps that bite and do not announce themselves: the registry is addressed as `localhost:5000` by
k3s and `172.17.0.1:5000` by Dagger (same container, different vantage point), and Dagger always
speaks HTTPS with no `--insecure`, which is why `make dagger-engine` exists. Re-run it after a Dagger
CLI upgrade.

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

**One thing left unproven on this host**, recorded so it is not claimed: the cadence skip was proven
via `compact_enabled: false` (`policy_disabled: 1` in the tick, `published` 570 → 569). The
`compact_interval_hours` path was NOT observed firing — the stamp probe did not land before the
migration. The planner code is identical for both; only the live proof of the interval arm is owed.
