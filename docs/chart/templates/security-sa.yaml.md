# chart/templates/security-sa.yaml

Rationale for `chart/templates/security-sa.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $components := list "catalog" "lineage" "web" "maintenance" "jobs" "viewer" "search" "annotator" "assist" "issuer-mirror" "hooks" }}`

ONE ServiceAccount PER FIRST-PARTY SERVICE, each with automountServiceAccountToken: false on the
SA object, so a pod holds a Kubernetes API token only where its own spec asks for one. The Dapr
injector's projected Sentry token is a separate volume and is unaffected.
These SAs carry no grant except issuer discovery below; a pod that calls the API keeps its own
RBAC-bound SA in its template. The fleet's SAs come from the same map fleet.yaml ranges, so a new
fleet service cannot name an SA this file did not render, and a pod names its SA with this file's
prefix (lance.fullname), which follows the release name where rask.fullname does not. Pinned by
tests/unit/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py.

## `"hooks"` in the component list

`<release>-sa-hooks`, the identity of the hooks that run before the release manifest (openfga-model at
pre-upgrade, object-store-scoped-users), is an ORDINARY ServiceAccount: an identity is never a hook ([[XC-126]]). A
hook-shaped SA, Role or binding has a lifecycle of its own, deleted by `hook-succeeded` while a failed hook's pod
still holds its token, so every later hook on it is refused, and it does not exist between upgrades. An ordinary SA
exists from the revision that introduces it on, so a pre-upgrade hook on any later upgrade finds it. The one upgrade
that introduces an ordinary SA runs its pre-upgrade hooks before the manifest creates it (measured 2026-10-02, rev
259: `serviceaccount "rask-sa-jobs" not found`): create it once beforehand and let `make k3s-up`'s
`--take-ownership` adopt it. Pinned by tests/unit/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py.

## `apiVersion: rbac.authorization.k8s.io/v1`

The SA issuer's discovery document and JWKS, which the API server serves only to a grant. The D1
verifiers (LH-220) read them to validate a caller's projected token offline; an in-cluster reader
authenticates this request with its own projected token. A third-party verifier that fetches
anonymously reads them through the issuer mirror (OpenFGA, XC-077); the object store's STS does not yet.
