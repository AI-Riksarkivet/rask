# chart/templates/prod-credentials.yaml

Rationale for `chart/templates/prod-credentials.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $v := .Values.AsMap -}}`

CREDENTIALS — no chart value carries one, and a real deployment carries no published dev secret.

Renders no object, on the `auth-consistency.yaml` precedent: it exists only to fail a render that
would otherwise install cleanly and be indefensible afterwards.

1. A CREDENTIAL PASSED AS A VALUE IS REFUSED ON EVERY RENDER ([[XC-004]]). A value is stored in the
release (`helm get values`), in every revision kept, and readable by anyone who can read the release, so
a secret there is a secret at rest outside the store. Every credential the estate uses is a field of the
store: the dev OpenBao's are generated in-cluster (external-secrets.yaml), a real deployment's are
provisioned by the platform. The keys below are the ones this chart once read; each refusal names the
store field that replaced it. `make k3s-converge` strips them from the release it carries forward.

2. A REAL DEPLOYMENT carries no published dev secret. The condition comes from `rask.isRealDeployment`,
so this template and the root-create lock (services.yaml, F2-11) cannot answer "is this real?"
differently. The signal is `openbao.enabled=false` (the platform's store), or `image.repository` with `image.localImages=false`:
"the images come from a registry someone else can reach" is a FACT about the deploy rather than a flag to
remember (`localhost:`, `127.0.0.1:` and `172.17.0.1:` registries are the in-cluster dev loop).

THE TWO EXEMPTIONS ARE FENCED HERE (owner, 2026-10-06): the bundled dev Dex (its static users, client
secret and publisher password are chart configuration, XC-025) and the dev OpenBao's root token (XC-079)
are the only credentials a render still carries, and only a dev render. A real deployment refuses the dev
OpenBao (which is also what stops the chart generating credentials for one: `lance.mintsDevCredentials`
needs `openbao.enabled`) and refuses the bundled Dex; it points at the platform's store and IdP.

A REAL DEPLOYMENT ALSO REFUSES THE DEVELOPMENT OBJECT STORE (`objectStore.enabled=true`, the rustfs fixture). Its
root secret is one the dev OpenBao generates, and on RustFS that secret signs every STS session token the store
issues (GHSA-m77q-r63m-pj89, ADR 0168), so a real estate on it would hold a trust root rask minted. Platform services
are external in production: a real deployment sets `objectStore.enabled=false` and `objectStore.externalEndpoint` to
the platform's S3, whose operator provisions rask's identities (`lance.s3Endpoint` fails the render when neither is
set).
