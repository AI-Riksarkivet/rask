# 0109. One Ray cluster on the latest release (R3, 2026-07-24)

Source: `docs/architecture/lance-ns-merge.md:441` (at `44b354f3`) (owner ruling R3, accepted 2026-07-24), with phase P5 at `:356-362`.

## Context

lance-ns ran its Lance jobs on a separate Ray cluster (`ray-lance`) at its own pins; rask ran a Ray estate for
GPU Serve work. A two-cluster option (Option B) was on the table.

## Decision

One Ray cluster, on the latest Ray release. Option B is revoked. Unification is the P1 pre-step, proven against
rask's own pipeline alone before any lance-ns code is grafted; P5 then folds the lance jobs onto that cluster
(the `ray-lance` image content merges into the unified image or a job runtime env, and
`deploy/ray-lance-demo.yaml` retires). GPU Serve and CPU stage-runner workloads share the one cluster.

## Consequences

- The fold is not finished. `deploy/ray-lance-demo.yaml` and `.docker/ray-lance.dockerfile` still exist, and
  `chart/values.yaml:1323` still documents `medallion.rayAddress=http://ray-lance-head:8265` as an override
  (`medallion.rayAddress` itself defaults empty, `chart/values.yaml:1362`). The R27 audit
  ([0122](0122-the-ray-plane-gets-a-standing-audit-r27-2026-07-28.md)) recorded the fold as the open R3 item.
- Per-workload images share the one cluster through `runtime_env.image_uri` (`CLAUDE.md`, `runners/`), which
  is how several workloads share it without sharing one environment.
