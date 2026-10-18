# chart/templates/openbao-seed.yaml

Rationale for `chart/templates/openbao-seed.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $signers := include "lance.signingIdentities" . | splitList " " | compact }}`

THE DEVELOPMENT STORE'S SEED, and only its seed. With `openbao.enabled` the server is the
`deploy/fixtures/openbao` release (`server -dev`: in memory, auto-unsealed, fixed root token, DEMO ONLY), and
rask renders the two scripts it runs as one ConfigMap, `<release>-openbao-seed`, which the fixture names as
`seedScripts`. The fixture pod runs `mint.sh` as an init container and `seed.sh` beside the server. What
the seed writes is rask's to decide, because it is the list of rask's own secrets (docs/architecture/deployment.md
lists the fields the platform's store must hold); how the dev server runs is the fixture's ([[XC-149]]).

The fixture mounts the scripts and both credential Secrets at 0444, not 0400 + an fsGroup: the seed (uid
65532) reads them, and an fsGroup would also make the pod's SA token 0640 root:<fsGroup>, which the server
cannot read once it drops to its own user, so its Kubernetes auth would review ESO's logins with ESO's own
token and refuse them (measured 2026-10-10, rev 293).

## `mint.sh: |`

MINT: a candidate pair per signing identity on a memory volume the server never mounts, and nsc and nk for
the seed's bus users (values.yaml `signing:`, `nats.auth`). Prints names only and always exits 0.

## `seed.sh: |`

THE POD SEEDS ITS OWN STORE. A seed that reaches the store through the Service writes to
whichever pod the Service routes to, and during a rollout that is the outgoing pod, whose
in-memory store is deleted with it. The script writes only to the server beside it, writes last the
key that server's readiness reads (`secret/openbao-seeded`), and stays running: a server restarted in
place comes back empty, and the loop at the end seeds it again with the same values. The fixture's
rolling update surges, so the outgoing pod serves until its replacement is seeded, and the replacement
carries the signing pairs and the bus trust root over from it through the Service.

## `set -e`

ERREXIT FOR THE WHOLE SCRIPT, never inside `seed`: POSIX ignores -e for every command of an
AND-OR list but the last, so `seed || exit 1` would run a failed write straight on to the
readiness key. Each check that may fail on purpose sits in an `if`, `until` or `&&`.

## `cd "$HOME"`

nsc reads its working directory, and the image's is not readable as this user (measured in nats-box 0.19.7).
The bao CLI also writes a token file under $HOME, which a read-only rootfs turns into a failure; the
fixture points HOME at its tmp emptyDir, and the signing pairs the seed keeps there outlive a restart of
either container.

## `{{- include "lance.signingShapes" . | nindent 4 }}`

ONE PAIR PER SIGNING IDENTITY, chosen from the kept copy, the store behind the Service, then the mint candidate
(values.yaml `signing:`). A value travels through shell variables, files and `@file` arguments, never argv or output.

## `nsc() { "$NATS_TOOLS/nsc" -H "$B/nsc" "$@"; }`

THE BUS CREDENTIALS (values.yaml `nats.auth`): the trust root and the route credential carried from the store behind the
Service or minted, then every user of the table issued afresh under that root, the whole bundle kept before the first write.

## `G=$DEV_CREDENTIALS`

EVERY GENERATED VALUE REACHES THE STORE AS `@file`, never argv, output or the render ([[XC-004]]):
the dev credentials from the ESO-generated Secret, and what is derived from them from files
written here. The two Dex values are the development IdP's own (deploy/fixtures/dex holds them, XC-025).

## `fingerprint() {`

THE MOUNTED CREDENTIALS ARE READ ON EVERY SEED, and a change to them is a reason to seed: kubelet
refreshes a mounted Secret in place, so rotating a key of `<release>-dev-credentials` (or the
operator's hf-token) reaches the store through the loop below without replacing the pod, whose
replacement without a surge would re-mint the signing pairs and the NATS root (OPERATORS.md 6-8).

## `dapr-app-token=@"$G/dapr-app-token" ray-auth-token=@"$G/ray-auth-token" \`

THE APP-API-TOKEN, so a pod can authenticate its Dapr doors without the value passing
through its environment (`RASK_APP_TOKEN_FROM_STORE`). ESO writes the same field into the
`dapr-app-token` Secret daprd stamps the header from, so the two agree by construction.

## `{{- if $.Values.objectStore.maintenanceAccessKey }}`

THE MAINTENANCE PLANE'S SCOPED S3 SECRET, when one is provisioned, as its own pair of fields in the
`lance` bundle. The bundle holds no root pair: the store's root lives at `secret/object-store-root`, which no Dapr
app may read (below). Seeded only when the access key is set, matching the Deployment's own `$scoped` test — a
field the service is told to read and nothing writes is a fail-closed boot, since
`fetch_required_secrets` raises rather than degrading.

## `{{- if $.Values.objectStore.catalogAccessKey }}`

THE CATALOG'S OWN GATE, matching the Deployment's own `$catalogScoped` test in
`services.yaml`, exactly as the lineage block below matches `$linScoped`. Gated on another
plane's flag, blanking that plane's access key while keeping the catalog's own identity
would leave the catalog told to read `catalog-s3-secret-key` and nobody writing it — a
fail-closed boot. Measured 2026-09-24 by rendering with the other plane's access key blanked:
the catalog Deployment carried `LANCE_DAPR_SECRET_S3_FIELD = "catalog-s3-secret-key"` while the
seed wrote only lineage's and maintenance's fields.

## `{{- if $.Values.objectStore.lineageAccessKey }}`

THE LINEAGE PLANE'S SCOPED S3 SECRET, on the same terms as the two above: its own pair of fields in the
`lance` bundle. Seeded only when the access key is set, matching the Deployment's own `$linScoped` test.

## `bao kv put secret/object-store-root object-store-access-key={{ $.Values.objectStore.accessKey }} \`

THE DEVELOPMENT STORE'S ROOT PAIR, at a path of its own and only with `objectStore.enabled`. On RustFS the root
secret signs every STS session token the store issues (GHSA-m77q-r63m-pj89, intentionally unfixed), so whoever reads it
can forge any credential the catalog vends (ADR 0168). Outside the `lance` bundle, no Dapr app reads it: every app's
Configuration denies `object-store-root` (`lance.daprAppSpec`), and ESO, whose policy below names the path, writes it
into `<release>-object-store-root` for the store and the scoped-users hook alone. Both halves sit in one object, as
the store's non-optional secret volume needs them. `tests/unit/test_the_root_storage_secret_reaches_only_its_users.py`
follows the root through every path a pod reads a secret by.

## `bao kv put secret/media-s3 access_key={{ . }} secret_key=@"$HOME/seed/scoped-{{ . }}" >/dev/null`

THE MEDIA CORPUS READERS' IDENTITY (values.yaml `objectStore.mediaAccessKey`), seeded when explorer serves a corpus
(`lance.mediaIdentity`). Annotator and search read it from their sidecar as `MEDIA_S3_SECRET_KEY=media-s3`
(explorer.yaml), with the `access_key`/`secret_key` fields the viewer's `viewer-s3` carries, and every other app's
Configuration denies it. Its secret is derived from the root in `derive()` by `lance.scopedSecretFn`, the function
the scoped-users hook runs, so the store's user and this field agree without either handing the other a key.

## `bao kv put secret/backup-s3 access_key={{ . }} secret_key=@"$HOME/seed/scoped-{{ . }}" >/dev/null`

THE BACKUPS' IDENTITY (values.yaml `objectStore.backupAccessKey`), seeded when either backup CronJob is on
(`lance.backupIdentity`) and derived in `derive()` like `media-s3`. The CronJobs carry no sidecar, so ESO reads this
path (the policy below names it) and writes `<release>-backup-s3`; every Dapr app's Configuration denies it.

## `c="$HOME/seed/otel-collector-export"`

THE COLLECTOR'S AUDIT-STORE CREDENTIAL ([[XC-003]]), create-if-absent: kept, else carried from the store behind
the Service, else minted.

## `bao kv put secret/viewer-s3 \`

THE OBJECT BROWSER'S CREDENTIALS, as their OWN secret rather than two more keys on
the `lance` bundle. `objects.py::_creds` fetches a per-STORE secret by name and
reads `access_key`/`secret_key` from it, so the store registry can point a store at
a credential without every reader of the shared bundle gaining it; with no `secret`
on a store the viewer would read process env, putting a root S3 key in the
environment of a pod that HAS a Dapr sidecar.

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
access, so `read` on `secret/data/lance`, on each NATS credential a pod without a
sidecar mounts (`lance.natsFiles`), and on each path an ExternalSecret fetches as a
whole (`object-store-root` with the dev store, `backup-s3` with a backup, the
Collector's and the hf-token's when they are seeded) is the whole grant — no list, no
other path, no write. `media-s3` and `viewer-s3` are absent: their readers have sidecars.
Zero trust applies to the thing fetching the secrets too.

EVERY STEP IS IDEMPOTENT: `auth enable` on an existing mount is the only one that
errors, and it is the expected outcome on re-run.

## `bao kv put secret/openbao-seeded pod="$HOSTNAME" >/dev/null`

READY ONCE SEEDED: the fixture's readiness probe reads this key, so the Service routes a sidecar
only to a store holding the whole seed, and a server restarted in place leaves the endpoints until it
is seeded again.
