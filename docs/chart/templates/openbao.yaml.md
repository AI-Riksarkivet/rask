# chart/templates/openbao.yaml

Rationale for `chart/templates/openbao.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $signers := include "lance.signingIdentities" . | splitList " " | compact }}`

OpenBao (the open-source Vault fork) — the secret store, fronted by a Dapr secretstores.hashicorp.vault
component (OpenBao is Vault-API compatible). openbao.devMode gates two paths:
  devMode=true  → `server -dev`: in-memory, auto-unsealed, fixed root token (DEMO ONLY).
  devMode=false → `server -config`: sealed, file-backed on a PVC; an operator must init + unseal it and
                  set up real auth (no fixed root token, no auto-seed). This is the prod path.

THE FIELDS `secret/lance` MUST HOLD (prod / externalAddr — nothing auto-seeds them and no credential is
a chart value, so an unseeded field is a fail-closed outage at first use, not a fallback):
minio-access-key, minio-secret-key, postgres-password, publisher-oidc-password,
publisher-oidc-client-secret (the Dex client secret), dapr-state-connection-string, dapr-app-token (the
app-API token: ESO writes it into the `dapr-app-token` Secret daprd stamps the header from, and the doors
compare against this field), frontend-session-secret (the zones' cookie sealing key), ray-auth-token,
ray-compute-access-key, ray-compute-secret-key, and `<plane>-s3-secret-key` for each scoped identity
named in values.yaml `minio` (sha256("<identity>-s3-<minio-secret-key>")[:40], `lance.scopedSecretFn`, which
`minio-scoped-users` derives too). With `secrets.hfTokenInStore`, `hf-token` (field `token`). Beside the bundle, for every identity in
lance.signingIdentities, two secrets: signing-public-<identity> (field `keys`) and then signing-key-<identity>
(field `seed`), described in values.yaml `signing:`; the render refuses a non-dev store until an operator
created them and signing.provisioned says so. Likewise nats-user-<user> per values.yaml `nats.auth` user, and
nats-route (and nats-server, in operator mode), attested by nats.auth.provisioned. With the in-chart Collector,
otel-collector-greptime (fields username, password), the credential the audit store admits. The devMode seed below is the
authoritative list — keep this comment and that `bao kv put` in sync.

TWO CONSUMERS, ONE KEY (2026-08-09). `publisher-oidc-client-secret` now serves the publish saga AND
the MinIO StatefulSet's STS AssumeRoleWithWebIdentity config — there is one Dex client (`dex.clientId`),
so there is one secret. The Tenant reaches it by a different ROUTE, not a different key: it is a
third-party operator-managed CR with no daprd sidecar, so it cannot read the Dapr secret store, and
it consumes the value as a `secretKeyRef` into the infra-credentials Secret (external-secrets syncs
that key FROM this one). Do NOT seed a second `dex-client-secret` field to tidy the naming: a
rotation that updates one copy and not the other breaks STS-assume on a path nothing tests.

## `strategy: { type: RollingUpdate, rollingUpdate: { maxSurge: 1, maxUnavailable: 0 } }`

SURGE BEFORE REMOVING: the seed carries the signing pairs over from the pod the Service still
routes to, which exists only while the outgoing pod serves until its replacement is Ready.

## `automountServiceAccountToken: true`

The Kubernetes auth backend reviews ESO's login with THIS pod's token and reads the API
server's CA beside it (openbao-auth-delegator.yaml). ESO is a prerequisite, so it always holds one.

## `- name: mint`

MINT: a candidate pair per signing identity on a memory volume the server never mounts, and nsc and nk for the seed's bus users
(values.yaml `signing:`, `nats.auth`). Prints names only and always exits 0.

## `httpGet:`

READY ONCE SEEDED: the seed container writes this key last, so the Service routes a sidecar
only to a store holding the whole seed, and a server restarted in place leaves the
endpoints until it is seeded again.

## `- name: seed`

THE POD SEEDS ITS OWN STORE. A seed that reaches the store through the Service writes to
whichever pod the Service routes to, and during a rollout that is the outgoing pod, whose
in-memory store is deleted with it. This container writes only to the server beside it,
writes last the key that server's readiness reads, and stays running: a server restarted in
place comes back empty, and the loop at the end seeds it again with the same values.

## `volumeMounts:`

the bao CLI writes a token file under $HOME, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses, and the signing pairs the
seed keeps there outlive a restart of either container.

## `set -e`

ERREXIT FOR THE WHOLE SCRIPT, never inside `seed`: POSIX ignores -e for every command of an
AND-OR list but the last, so `seed || exit 1` would run a failed write straight on to the
readiness key. Each check that may fail on purpose sits in an `if`, `until` or `&&`.

## `cd "$HOME"`

nsc reads its working directory, and the image's is not readable as this user (measured in nats-box 0.19.7).

## `{{- include "lance.signingShapes" . | nindent 14 }}`

ONE PAIR PER SIGNING IDENTITY, chosen from the kept copy, the store behind the Service, then the mint candidate
(values.yaml `signing:`). A value travels through shell variables, files and `@file` arguments, never argv or output.

## `nsc() { "$NATS_TOOLS/nsc" -H "$B/nsc" "$@"; }`

THE BUS CREDENTIALS (values.yaml `nats.auth`): the trust root and the route credential carried from the store behind the
Service or minted, then every user of the table issued afresh under that root, the whole bundle kept before the first write.

## `G=$DEV_CREDENTIALS`

EVERY GENERATED VALUE REACHES THE STORE AS `@file`, never argv, output or the render ([[XC-004]]):
the dev credentials from the ESO-generated Secret, and what is derived from them from files
written here. The two Dex values are the bundled IdP's own (dex.yaml carries them, XC-025).

## `fingerprint() {`

THE MOUNTED CREDENTIALS ARE READ ON EVERY SEED, and a change to them is a reason to seed: kubelet
refreshes a mounted Secret in place, so rotating a key of `<release>-dev-credentials` (or the
operator's hf-token) reaches the store through the loop below without replacing this pod, whose
replacement without a surge would re-mint the signing pairs and the NATS root (OPERATORS.md 6-8).

## `dapr-app-token=@"$G/dapr-app-token" ray-auth-token=@"$G/ray-auth-token" \`

THE APP-API-TOKEN, so a pod can authenticate its Dapr doors without the value passing
through its environment (`RASK_APP_TOKEN_FROM_STORE`). ESO writes the same field into the
`dapr-app-token` Secret daprd stamps the header from, so the two agree by construction.

## `ray-compute-access-key={{ .Values.minio.rayComputeAccessKey | default .Values.minio.accessKey }} \`

THE COMPUTE PLANE'S SCOPED PAIR, which `external-secrets.yaml` READS into
`infra-credentials`. ESO syncs a Secret whole or not at all, so a property it reads and
nothing writes takes every pod mounting that Secret down (measured 2026-09-09: the
AGE consumers, the OpenFGA migrate Job, the bucket-init Job, the OTel Collector and
all seven web zones in CreateContainerConfigError).

## `{{- if $.Values.minio.maintenanceAccessKey }}`

THE MAINTENANCE PLANE'S SCOPED S3 SECRET, when one is provisioned. Its own field
rather than a second value on `minio-secret-key`: that field IS the tenant root's
secret and other services read it, so overwriting it would repoint the whole
estate at a credential scoped to maintenance's prefixes. Seeded only when both
halves of the pair are set, matching the Deployment's own `$scoped` test — a
field the service is told to read and nothing writes is a fail-closed boot, since
`fetch_required_secrets` raises rather than degrading.

## `{{- if $.Values.minio.medallionAccessKey }}`

THE MEDALLION PLANE'S SCOPED S3 SECRET, on the same terms as maintenance's: its own
field rather than a second value on `minio-secret-key`, which IS the tenant root's
secret that other services read. Seeded only when both halves of the pair are set,
matching the Deployment's own `$medScoped` test — a field the service is told to
read and nothing writes is a fail-closed boot, since `fetch_required_secrets`
raises rather than degrading.

## `{{- if $.Values.minio.catalogAccessKey }}`

THE CATALOG'S OWN GATE, matching the Deployment's own `$catalogScoped` test
(`services.yaml:129`) exactly as the medallion block above matches `$medScoped`
and the lineage block below matches `$linScoped`. It rode the MEDALLION's flag,
so blanking `minio.medallionAccessKey` while keeping the catalog's own identity
left the catalog told to read `catalog-s3-secret-key` and nobody writing it —
which the comment above already names as a fail-closed boot, since
`fetch_required_secrets` raises rather than degrading. Measured 2026-09-24 by
rendering `--set minio.medallionAccessKey=`: the catalog Deployment carries
`LANCE_DAPR_SECRET_S3_FIELD = "catalog-s3-secret-key"` while the seed writes
only lineage's and maintenance's fields. One service's identity could take
another service down at boot.

## `{{- if $.Values.minio.lineageAccessKey }}`

THE LINEAGE PLANE'S SCOPED S3 SECRET, on the same terms as the two above: its own
field rather than a second value on `minio-secret-key`, which IS the tenant root's
secret that other services read. Seeded only when the access key is set, matching
the Deployment's own `$linScoped` test — a field the service is told to read and
nothing writes is a fail-closed boot, since `fetch_required_secrets` raises rather
than degrading.

## `c="$HOME/seed/otel-collector-greptime"`

THE COLLECTOR'S AUDIT-STORE CREDENTIAL ([[XC-003]]), create-if-absent: kept, else carried from the store behind
the Service, else minted.

## `bao kv put secret/viewer-s3 \`

THE OBJECT BROWSER'S CREDENTIALS, as their OWN secret rather than two more keys on
the `lance` bundle. `objects.py::_creds` fetches a per-STORE secret by name and
reads `access_key`/`secret_key` from it, so the store registry can point a store at
a credential without every reader of the shared bundle gaining it. That is the
mechanism the viewer already had and did not use: with no `secret` on a store it
fell back to process env, which is how `AWS_ACCESS_KEY_ID=rustfsadmin` came to sit
in its environment on a pod that HAS a Dapr sidecar.

## `{{- range $ref, $_ := $baseFixture }}`

values.yaml `catalog.multibase.devSeed`: each data base's credential, under the name its reference gives.

## `bao auth enable -path={{ $.Values.externalSecrets.kubernetesAuthPath }} kubernetes 2>/dev/null || echo "kub...`

THE AUTH HALF ESO NEEDS, and without it the KV half above is unreachable.
`external-secrets.yaml` renders a SecretStore that authenticates to OpenBao with
`auth.kubernetes` at mountPath `{{ $.Values.externalSecrets.kubernetesAuthPath }}`
assuming role `{{ $.Values.externalSecrets.role }}`. Measured 2026-09-08: the
operator ran 3/3 with ZERO SecretStores in the cluster while nothing enabled that
auth backend or created that role, so every ExternalSecret sat in SecretSyncedError,
which reads as a broken deploy rather than a missing prerequisite.

IT BELONGS IN THE SEED because this is the one place that already holds the root
token and already writes the exact path the role must be allowed to read; a
separate bootstrap would need a second copy of both.

THE POLICY IS SCOPED TO NAMED PATHS, not the mount. ESO is a controller with standing
access, so `read` on `secret/data/lance`, and on each NATS credential a pod without a
sidecar mounts (`lance.natsFiles`), is the whole grant — no list, no other path, no
write. Zero trust applies to the thing fetching the secrets too.

EVERY STEP IS IDEMPOTENT: `auth enable` on an existing mount is the only one that
errors, and it is the expected outcome on re-run.

## `- { name: dev-credentials, secret: { secretName: {{ include "lance.fullname" . }}-dev-credentials, defaultM...`

GENERATED IN THE CLUSTER by ESO (external-secrets.yaml); the pod starts once it exists.

0444, not 0400 + an fsGroup: the seed (uid 65532) reads these, and an fsGroup would also make the pod's SA token
0640 root:<fsGroup>, which the server cannot read once it drops to its own user, so its Kubernetes auth would
review ESO's logins with ESO's own token and refuse them (measured 2026-10-10, rev 293). The pod's containers
are the store's own server, mint and seed.

## `- { name: supplied-credentials, secret: { secretName: {{ include "lance.fullname" . }}-supplied-credentials...`

THE OPERATOR'S BOOTSTRAP for third-party credentials (`make k3s-up` writes it from HF_TOKEN).

## `(end of file)`

openbao.enabled
