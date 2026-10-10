# 0009. §12 — prod-hardening backlog (native switches off)

**Decision.** Several native k8s/Dapr security switches are deliberately **off** in the dev baseline,
deferred to prod in a specific fix-order: **L3 network default-deny** first (today any pod can reach the
OpenBao secret store), then **least-privilege ServiceAccounts** (~13 pods run on `default` with a mountable
API token), then **infra-pod securityContext** (the app tier is already hardened), then **Pod Security
Admission** enforcement.

**Rationale.** The don't-reinvent audit confirmed we reinvent nothing k8s/Dapr owns (zero code to delete);
these are un-flipped native switches, not missing code, and they are footgun-sequenced — default-deny egress
without a kube-dns allow bricks the cluster, and restricted PSA would reject `lineage`/`openfga-migrate`
until their root init containers are hardened. kind's default CNI ignores NetworkPolicy, so they cannot even
be validated in the dev baseline.
