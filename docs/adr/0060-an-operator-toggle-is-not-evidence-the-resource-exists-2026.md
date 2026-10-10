# 0060. An operator toggle is not evidence the resource exists (2026-09-07)

`CLAUDE.md` stated that the lineage (AGE) and OpenFGA Postgres databases are CloudNativePG. They are
not, and have not been. Measured live: **zero** `cluster.postgresql.cnpg.io` objects, `rask-age-0`
owned by `StatefulSet/rask-age`, and `age.cnpgCluster.enabled` defaulting **false** — with the
chart's own reason, that the cutover needs a built AGE extension image, K8s 1.33+ and CNPG >= 1.27.
`age-cluster.yaml` fails the render if both paths are on, so the chart was never ambiguous.

**What made the claim survive is that a component by the right name is running.** `cnpg.enabled:
true` installs the OPERATOR subchart, `rask-cloudnative-pg` appears in `kubectl get deploy`, and the
values file talks about Clusters. So a reader asking "are our databases CNPG-managed?" finds
confirmation everywhere except the one place that answers it — the list of `Cluster` objects, which
is empty. The operator has been reconciling nothing for as long as the estate has existed.

**The rule this adds: an operator's toggle and its custom resource's gate are different values, and
only the second decides whether the resource exists.** The chart already knew this — `cnpg.enabled`
and `age.cnpgCluster.enabled` are separate on purpose, and `values.yaml` says so. The doc collapsed
them, which is the same collapse as reading `provenance()` as provenance or a passed field as a read
one.

It also changes F2-6's shape. "The AGE DSN carries no `sslmode`" looks like a connection-string fix
until you ask the server: `SHOW ssl` answers **off**, so requiring TLS from the client alone would be
an outage. The store has no certificate because nothing issues one — which is exactly what a CNPG
Cluster would have done, and is why believing the store was CNPG made the gap invisible.
