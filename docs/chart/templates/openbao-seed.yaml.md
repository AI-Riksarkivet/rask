# chart/templates/openbao-seed.yaml

Rationale for `chart/templates/openbao-seed.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- $signers := include "lance.signingIdentities" . | splitList " " | compact }}`

THE DEVELOPMENT STORE'S SEED, and only its seed. With `openbao.enabled` the server is the
`deploy/fixtures/openbao` release (in memory, DEMO ONLY), and rask renders the two scripts it runs as one ConfigMap,
`<release>-openbao-seed`, which the fixture names as `seedScripts`; the store's roles and policies are another,
`<release>-secret-store-identities` (secret-store-identities.yaml), which it names as `storeIdentities`. The fixture
pod runs `mint.sh` as an init container and `open-and-seed.sh` beside the server. What the seed writes, and who may read it, is
rask's to decide, because it is the list of rask's own secrets and readers (docs/architecture/deployment.md lists what
the platform's store must hold); how the server runs is the fixture's ([[XC-149]]).

The fixture mounts the scripts, the identities and both credential Secrets at 0444, not 0400 + an fsGroup: the seed (uid
65532) reads them, and an fsGroup would also make the pod's SA token 0640 root:<fsGroup>, which the server
cannot read once it drops to its own user, so its Kubernetes auth would review ESO's logins with ESO's own
token and refuse them (measured 2026-10-10, rev 293).

## `mint.sh: |`

MINT: a candidate pair per signing identity on a memory volume the server never mounts, and nsc and nk for
the seed's bus users (values.yaml `signing:`, `nats.auth`). Prints names only and always exits 0.

## `open-and-seed.sh: |`

THE POD SEEDS ITS OWN STORE. A seed that reaches the store through the Service writes to
whichever pod the Service routes to, and during a rollout that is the outgoing pod, whose
in-memory store is deleted with it. The script writes only to the server beside it, writes last the
key that server's readiness reads (`secret/openbao-seeded`), and stays running: a server restarted in
place comes back empty, and the loop at the end seeds it again with the same values. The fixture's
rolling update surges, so the outgoing pod serves until its replacement is seeded, and the replacement
carries the signing pairs and the bus trust root over from it through the Service.

ITS NAME IS PART OF ITS POD'S CONTRACT. A running seed executes a changed script again in place and leaves a script
that is gone alone (`[ -f "$0" ]` in the loop at the end), so a change the running pod can serve reaches it with no
rollout. A script that needs what the running pod lacks (an env var, a mount) takes a new name, as this one does for
`STORE_ROOT_TOKEN`, `STORE_KEYS` and `STORE_IDENTITIES`: under the old name the outgoing pod would run it, fail its
first check and leave the Service before its replacement had carried the signing pairs and the bus trust root from it.

## `set -e`

ERREXIT FOR THE WHOLE SCRIPT, never inside `seed`: POSIX ignores -e for every command of an
AND-OR list but the last, so `seed || exit 1` would run a failed write straight on to the
readiness key. Each check that may fail on purpose sits in an `if`, `until` or `&&`.

## `open_store() {`

THE SEED INITIALIZES ITS OWN SERVER ([[XC-165]], ADR 0169). The fixture's server is not `server -dev`, which prints its
root token and unseal key to its log (openbao 2.2.0 `command/server.go:2720,2732`), where anyone who can read logs, the
Collector's filelog included, would hold the store; it starts uninitialized and in memory, and logs only that a root
token was generated (measured 2026-10-18). `bao operator init` answers into the seed's memory volume (`STORE_KEYS`), the
seed unseals with the key on stdin, uses the init's root token to mint the store's administrator under the id the
fixture generates in-cluster (`STORE_ROOT_TOKEN`, a Secret mounted into this container alone; the JSON on stdin, so
the id is in no argument), revokes the init token, and mounts the KV v2 engine at `externalSecrets.kvMount`, which a
store outside dev mode starts without. Every step
re-checks what is already done, so a seed container that restarts part-way through finishes the same init; a server
that restarts comes back uninitialized and is initialized again under the same administrator, so a surge's new pod
still reads its outgoing one with it.

A SEALED STORE IS LEFT SEALED once its init has finished: a drill or an operator sealed it, and the seed waits for the
unseal (`tests/e2e-py/chaos.py`, which reads the key the seed keeps).

## `cd "$HOME"`

nsc reads its working directory, and the image's is not readable as this user (measured in nats-box 0.19.7).
The bao CLI reads its token from `$HOME/.vault-token`, which the seed links to the administrator token the fixture
mounts, so every `bao` in this container (the seed's, its readiness probe's, an operator's `kubectl exec`) is the
administrator and no token rides an env value or an argument. The fixture points HOME at its tmp emptyDir, and the
signing pairs the seed keeps there outlive a restart of either container.

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

## `bao kv put secret/postgres password=@"$G/postgres-password" >/dev/null`

ONE SECRET PER READER SET ([[XC-165]]): a store and Dapr both grant by secret name, so a field of a shared bundle is read
by everyone who may read the bundle. Each secret here has exactly the readers `lance.storeReads` and `lance.esoReads`
give it, and two readers of one value read one secret, never a copy.

## `bao kv put secret/dapr-app-token token=@"$G/dapr-app-token" >/dev/null`

THE APP-API TOKEN, so a pod can authenticate its Dapr doors without the value passing through its environment
(`RASK_APP_TOKEN_FROM_STORE`). ESO writes the same field into the `dapr-app-token` Secret daprd stamps the header from,
so the two agree by construction. One estate-wide token, read by the apps with a Dapr door and by ESO.

## `{{- range $plane, $id := dict "maintenance" .Values.objectStore.maintenanceAccessKey "lineage" .Values.objectStore.lineageAccessKey }}`

EACH PLANE'S SCOPED S3 PAIR, as its own secret `<plane>-s3` with the `access_key`/`secret_key` fields every scoped pair
carries, readable by that app alone. The catalog has none: its store identity is its ServiceAccount token (ADR 0170).
No secret holds a root pair: the store's root lives at `secret/object-store-root`, which only ESO reads (below). Seeded
only when the access key is set, and a blank one fails the render
(`tests/unit/test_a_blank_store_identity_fails_the_render.py`), so a field a service is told to read is always written.

## `bao kv put secret/object-store-root object-store-access-key={{ $.Values.objectStore.accessKey }} \`

THE DEVELOPMENT STORE'S ROOT PAIR, at a path of its own and only with `objectStore.enabled`. On RustFS the root
secret signs every STS session token the store issues (GHSA-m77q-r63m-pj89, intentionally unfixed), so whoever reads it
can forge any credential the catalog vends (ADR 0168). No Dapr app reads it: no role's policy but ESO's grants it, and
ESO writes it into `<release>-object-store-root` for the store and the scoped-users hook alone. Both halves sit in one
object, as the store's non-optional secret volume needs them.
`tests/unit/test_every_secret_in_the_dev_store_has_exactly_its_readers.py` follows it through every path a pod reads a
secret by.

## `bao kv put secret/backup-s3 access_key={{ . }} secret_key=@"$HOME/seed/scoped-{{ . }}" >/dev/null`

THE BACKUPS' IDENTITY (values.yaml `objectStore.backupAccessKey`), seeded when either backup CronJob is on
(`lance.backupIdentity`) and derived in `derive()` like the other scoped identities. The CronJobs carry no sidecar, so ESO reads this
path (its policy names it) and writes `<release>-backup-s3`; no app's policy names it.

## `c="$HOME/seed/otel-collector-export"`

THE COLLECTOR'S AUDIT-STORE CREDENTIAL ([[XC-003]]), create-if-absent: kept, else carried from the store behind
the Service, else minted.

## `i="$HOME/seed/commit-id-key"`

LH-497: the key the catalog derives every announcement's run id under, which only the catalog reads (`lance.storeReads`). Carried from the store behind the Service when a pod replaces this one, like a signing pair, because it is never rotated: an id derived under a new key would announce every commit still unconfirmed a second time. Minted as 32 random bytes of hex only when no store serves one. A platform store's operator provisions `commit-id-key` itself; the catalog is not Ready until it reads one.

## `m="$HOME/seed/{{ .secretName }}"`

THE EMAIL CHANNEL'S SMTP CREDENTIAL ([[CTL-30]]), seeded only while `services.notifications.channels.email` is on,
under the same create-if-absent rule as the Collector's. The `notifications-email` Component resolves its `user` and
`password` in the notifications sidecar, whose store policy names it through `lance.storeReads`. The dev relay
(the `mailpit` fixture) asks for no authentication, so the dev pair is one only the store holds; a platform's relay
gets the pair its operator puts in the platform's store.

## `bao auth enable -path={{ $.Values.externalSecrets.kubernetesAuthPath }} kubernetes 2>/dev/null || echo "kub...`

THE STORE'S KUBERNETES AUTH, which every identity that reads it logs in through: ESO's SecretStore, as the role
`{{ $.Values.externalSecrets.role }}` bound to ESO's ServiceAccount, and each app's sidecar, as the role
`<release>-<app id>` bound to that app's ServiceAccount and to the audience its pod's projected token carries
([[XC-165]]). Measured 2026-09-08: the operator ran 3/3 with ZERO SecretStores in the cluster while nothing enabled that
auth backend or created that role, so every ExternalSecret sat in SecretSyncedError, which reads as a broken deploy
rather than a missing prerequisite.

IT BELONGS IN THE SEED because this is the one place that already holds the store's administrator token and already
writes the paths the roles must be allowed to read; a separate bootstrap would need a second copy of both. The server
reviews each login's token against the API server with its own, through `system:auth-delegator` (the fixture's
ServiceAccount).

## `for doc in "$STORE_IDENTITIES"/policy-*.json; do`

EVERY ROLE AND POLICY IS A DOCUMENT rask renders for any store (secret-store-identities.yaml, mounted at
`STORE_IDENTITIES`), applied as it is: the render gate reads the same documents to decide who can read each secret, and
a platform store's operator creates the same roles and policies from them.

EVERY STEP IS IDEMPOTENT: `auth enable` on an existing mount is the only one that errors, and it is the expected
outcome on re-run; a policy or a role written again replaces the one the store holds, so a changed document reaches
the store on the next seed (the loop below re-runs the seed when any rendered file changes).

## `bao kv put secret/openbao-seeded pod="$HOSTNAME" >/dev/null`

READY ONCE SEEDED: the seed container's readiness probe reads this key with the administrator token, so the Service
routes a sidecar only to a store holding the whole seed, and a server restarted in place leaves the endpoints until it
is seeded again. The server's own probe reads `sys/health`, which needs no token.
