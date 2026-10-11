# chart/templates/security-sa.yaml

Rationale for `chart/templates/security-sa.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $components := list "catalog" "lineage" "web" "maintenance" "medallion-producer" "minio" "age" "openbao...`

ONE ServiceAccount PER FIRST-PARTY SERVICE, each with automountServiceAccountToken: false on the
SA object, so a pod holds a Kubernetes API token only where its own spec asks for one (OpenBao
under ESO). The Dapr injector's projected Sentry token is a separate volume and is unaffected.
These SAs carry no grant except issuer discovery below; a pod that calls the API keeps its own
RBAC-bound SA in its template. The fleet's SAs come from the same map fleet.yaml ranges, so a new
fleet service cannot name an SA this file did not render, and a pod names its SA with this file's
prefix (lance.fullname), which follows the release name where rask.fullname does not. Pinned by
tests/unit/test_a_first_party_pod_cannot_read_a_secret_through_the_kube_api.py.

## `apiVersion: v1`

The identity of the hooks that run BEFORE the release manifest (openfga-model, minio-scoped-users).
Helm applies the manifest after its pre-upgrade hooks, so an ordinary SA does not exist yet on the
upgrade that introduces it, and the hook's pod is refused at admission until the upgrade times out
(measured 2026-10-02, rev 259: `serviceaccount "rask-sa-jobs" not found`). A hook of its own at a
lower weight exists in every phase they run in; kueue-queues.yaml does the same.

## `apiVersion: rbac.authorization.k8s.io/v1`

The SA issuer's discovery document and JWKS, which the API server serves only to a grant. The D1
verifiers (LH-220) read them to validate a caller's projected token offline; an in-cluster reader
authenticates this request with its own projected token. A third-party verifier that fetches
anonymously reads them through the issuer mirror (OpenFGA, XC-077); MinIO STS does not yet.
