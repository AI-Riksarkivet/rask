# chart/templates/prod-credentials.yaml

Rationale for `chart/templates/prod-credentials.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $v := .Values.AsMap -}}`

CREDENTIALS — no chart value carries one, and a real deployment carries no published dev secret.

Renders no object: it exists only to fail a render that
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

THE ONE EXEMPTION IS FENCED HERE (owner, 2026-10-06): the bundled dev Dex (its static users and client secret
are chart configuration, XC-025) is the only credential a render still carries, and only a dev
render; the dev OpenBao's administrator token is generated in-cluster and held by its seed alone ([[XC-165]]). A real deployment refuses the dev
OpenBao (which is also what stops the chart generating credentials for one: `lance.mintsDevCredentials`
needs `openbao.enabled`) and refuses the bundled Dex; it points at the platform's store and IdP.

A REAL DEPLOYMENT ALSO REFUSES THE DEVELOPMENT OBJECT STORE (`objectStore.enabled=true`, the rustfs fixture). Its
root secret is one the dev OpenBao generates, and on RustFS that secret signs every STS session token the store
issues (GHSA-m77q-r63m-pj89, ADR 0168), so a real estate on it would hold a trust root rask minted. Platform services
are external in production: a real deployment sets `objectStore.enabled=false` and `objectStore.externalEndpoint` to
the platform's S3, whose operator provisions rask's identities (`lance.s3Endpoint` fails the render when neither is
set).

## `{{- $local := ternary ... .Values.openbao.enabled -}}`

The way out for an estate that is local after all follows the signal that made the render real: with the platform's
store named (`openbao.enabled=false`), only the development store with side-loaded or `localhost:` images renders as
local; with images from a registry off this node, side-loading them or a `localhost:` registry does.

## `{{- if not .Values.auth.enabled -}}`

3. A REAL DEPLOYMENT IS REFUSED WHAT WOULD EXPOSE IT ONCE INSTALLED ([[XC-184]]), on the same `rask.isRealDeployment`
answer, and joins the dev credentials above in the one refusal, so an operator reads every offending value from one
render:

- `auth.enabled=false`. Off, the chart writes `RASK_INSECURE_ALLOW_UNAUTHENTICATED` on the services' behalf
  (`lance.governedAuthAck`), the acknowledgement `assert_authentication_configured` refuses to boot without. On a real
  deployment that would open every governed door with nobody having said so; the open profile stays a development
  render's.
- `dapr.dashboard.enabled=true`, read as the template renders it (`lance.daprDashboardEnabled`). The Dapr dashboard
  authenticates nobody, and its ClusterRole reads every pod and its log in the cluster (`dapr-dashboard.yaml`). It is a
  development tool: off in values.yaml, on in `deploy/values-local.yaml`.

## `{{- $hops := list -}}`

A PLATFORM ADDRESS THE CHART NAMES IS HTTPS ON A REAL DEPLOYMENT. Each hop is listed with what would cross it:

- `auth.fgaApiUrl`, read while `openfga.enabled` is off: the projected `rask-openfga` tokens of the catalog, maintenance
  and the FGA hooks. OpenFGA authorizes none of its callers, so a token read on the path writes any tuple.
- `objectStore.externalEndpoint`: the catalog's projected ServiceAccount token, exchanged at that STS for every session
  it holds and vends (ADR 0170), and each session's secret key in the answer.
- `vending.endpoint`: every vended session's requests and the tables' bytes. The vend sets `allow_http=true` whenever
  its endpoint's scheme is http (`lakehouse.endpoint_scheme.allow_http_for`), which turns off Lance's TLS-only default
  for that client (`lance_docs/lance/guide/object_store.md`: `allow_http`, default `False`).
- `observability.export.endpoint`, read while `observability.enabled`: the export credential, which the Collector's
  basic auth sends with every batch (`otel-collector.yaml`).
- `services.lineage.forward.url`: every event the estate's provenance records, and the backend's API key when
  `forward.authSecret` names one.

A development estate reaches its fixtures over http (the store's node port, GreptimeDB, Marquez), so the rule follows
`rask.isRealDeployment` rather than refusing every http address. The platform's secret store (`lance.platformStoreAddr`)
and IdP (`lance.oidcIssuer`) are refused http by their own helpers on any render that names them. The release's own
Services (the bundled OpenFGA, the issuer mirror) are in-cluster hops and not judged here; nor are the bus (`nats.url`)
and the Postgres (`age.externalHost`), which speak their own protocols.
