# chart/templates/openbao.yaml

Rationale for `chart/templates/openbao.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- if and .Values.openbao.enabled (not .Values.openbao.devMode) }}`

OpenBao (the open-source Vault fork) — the secret store, fronted by a Dapr secretstores.hashicorp.vault
component (OpenBao is Vault-API compatible). In production the store is the platform's, named by
`openbao.externalAddr`, and rask renders no server at all. This template is the in-chart sealed server
for `devMode=false`: `server -config`, file-backed on a PVC; an operator must init + unseal it and set up
real auth (no fixed root token, no auto-seed). The `devMode=true` server is not rendered here: it is the
development fixture `deploy/fixtures/openbao`, and rask renders only the scripts it runs
(`openbao-seed.yaml`).

THE FIELDS `secret/lance` MUST HOLD (prod / externalAddr — nothing auto-seeds them and no credential is
a chart value, so an unseeded field is a fail-closed outage at first use, not a fallback):
minio-access-key, minio-secret-key, postgres-password, publisher-oidc-password,
publisher-oidc-client-secret (the Dex client secret), dapr-state-connection-string, dapr-app-token (the
app-API token: ESO writes it into the `dapr-app-token` Secret daprd stamps the header from, and the doors
compare against this field), frontend-session-secret (the zones' cookie sealing key), ray-auth-token,
and `<plane>-s3-secret-key` for each scoped identity
named in values.yaml `minio` (sha256("<identity>-s3-<minio-secret-key>")[:40], `lance.scopedSecretFn`, which
`minio-scoped-users` derives too). With `secrets.hfTokenInStore`, `hf-token` (field `token`). Beside the bundle, for every identity in
lance.signingIdentities, two secrets: signing-public-<identity> (field `keys`) and then signing-key-<identity>
(field `seed`), described in values.yaml `signing:`; the render refuses a non-dev store until an operator
created them and signing.provisioned says so. Likewise nats-user-<user> per values.yaml `nats.auth` user, and
nats-route (and nats-server, in operator mode), attested by nats.auth.provisioned. With the in-chart Collector,
otel-collector-export (fields username, password), the credential the audit store admits. The dev seed in
`openbao-seed.yaml` is the authoritative list — keep this section and its `bao kv put` in sync.

SEVERAL CONSUMERS, ONE KEY (2026-08-09). `publisher-oidc-client-secret` serves the annotator's publish
client (`explorer.yaml`), the zones' OIDC client (the frontend-session ExternalSecret in
`external-secrets.yaml`) AND, with `minio.oidc.enabled`, the object store's STS AssumeRoleWithWebIdentity config —
there is one Dex client (`auth.oidc.clientId`), so there is one secret. The object store reaches it by a
different ROUTE, not a different key: it has no daprd sidecar, so it cannot read the Dapr secret store,
and it consumes the value as a `secretKeyRef` into the infra-credentials Secret (external-secrets syncs
that key FROM this one). Do NOT seed a second `dex-client-secret` field to tidy the naming: a rotation
that updates one copy and not the other breaks STS-assume on a path nothing tests.

## `automountServiceAccountToken: true`

The Kubernetes auth backend reviews ESO's login with THIS pod's token and reads the API
server's CA beside it (openbao-auth-delegator.yaml). ESO is a prerequisite, so it always holds one.
