# 0038. Helm release storage: the SQL driver stands; the chart is NOT split (2026-08-15)

**Decision.** Keep `HELM_DRIVER=sql`. Do not split the chart into infra + app now.

**What forced the choice.** `helm upgrade` began failing outright:

```
Secret "sh.helm.release.v1.rask.v35" is invalid: data: Too long: may not be more than 1048576 bytes
```

Helm embeds the WHOLE CHART in every revision, and ~880 KB of `chart/charts/*.tgz` is
already-compressed subchart archives that gzip cannot shrink further. Measured: v28 964 KB → v34
1,046.9 KB → v35 1,048.5 KB against Kubernetes' 1,024 KB object limit. It had been creeping for
months; v35 simply crossed it. Nothing could be deployed — not `make k3s-up`, not CI.

**Four alternatives were measured and rejected, so nobody re-tries them:**

| attempt | result |
| --- | --- |
| convert rendered YAML comments to Helm template comments | broke the render TWICE — ate content inside block scalars (`- \|`, ConfigMap SQL). Reverted both times |
| drop an unused subchart | all ten are genuinely enabled |
| unpack `charts/*.tgz` so gzip can compress them | packed 871.9 KB vs unpacked 882.8 KB — **costs 11 KB** |
| `kueue.enabled=false` | buys 184 KB, and Kueue is provably idle (0 workloads; the only `queue-name` reference in the repo is a comment) — but deletes an operator installed on purpose, to dodge a storage limit, at the wrong layer |

**Why not split the chart now.** It is the architecturally correct answer and remains the intended end
state — infra (operators + CRDs, installed rarely) separated from app (upgraded constantly) would put
the app release back under the Secret limit and delete the operational cost below. It is also a large,
risky change to the one artefact that deploys both local k3s and production, and the SQL driver
removed the blockage in one command with zero resource churn.

**The cost this accepts, stated plainly.** Every `helm` invocation must carry `HELM_DRIVER=sql` and the
DSN, or Helm reads the empty Secret backend, reports the release ABSENT, and `upgrade --install`
re-installs over a live estate without erroring. That is mitigated — not removed — by routing every
call through `scripts/helm.sh`, which fails closed when it cannot resolve the address rather than
falling back. CI still needs the same treatment.

**The trigger that reopens this.** Split the chart when any of these holds: CI needs to deploy and
cannot carry the driver config; the Postgres holding the release becomes a dependency for a
disaster-recovery path that must not require it; or a second estate needs the chart and inherits the
env-var contract. Until then the driver is the answer and the split is a known, costed improvement.
