# Operators: what the platform runs, and what rask never builds

Which Kubernetes operators sit around rask, who installs them, and the operational procedures rask itself owns
(secrets, signing keys, NATS users, credentials, § 5 onward).

## 1 · Prerequisites are the platform's

The product chart installs rask and nothing else ([ADR 0153](adr/0153-the-app-chart-is-the-product.md)). Every
operator and control plane rask relies on is a prerequisite the platform runs, named by reference; development
installs each from `deploy/fixtures`, its own Helm release, before rask's (`make k3s-up`;
[architecture/deployment.md](architecture/deployment.md)):

| Prerequisite | What rask renders against it |
|---|---|
| **External Secrets Operator** | the ExternalSecrets and SecretStore every pod without a sidecar takes its credentials from (§ 8) |
| **Dapr control plane** | the Components, Configurations, Resiliency and Subscriptions, and the sidecar annotations |
| **NATS JetStream** | the stream Job that creates rask's streams, and one bus user per app (§ 7) |
| **CloudNativePG operator** | a `Cluster` only with `age.cnpgCluster.enabled`; see [`CNPG-AGE.md`](CNPG-AGE.md) |
| **The OTel backend** | the Collector's export endpoint and headers; the store, dashboards and alerting are the platform's |

The lakehouse runs no compute and submits no job ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)):
KubeRay, Kueue, GPU operators and whatever orchestrates a pipeline belong to the user who runs that pipeline, and no
lakehouse service imports `ray` (the import contract `the-lakehouse-knows-no-client`).

## 2 · What we never build: a custom lakehouse operator

An operator earns its complexity when state of record lives in CRDs and needs reconciling. Ours does not: catalog and
lakehouse state, lineage's commit feed and edge list included, lives in **Lance manifests on S3**, authz in
**Postgres** (OpenFGA, on the `rask-age` server), and every
control loop we need is either an existing service (maintenance's work queue), an existing Job (stream, bucket and seed
provisioning), or one of the operators above. A lakehouse operator would be a second reconciler over state that
already has an owner.

## 3 · Optional: NACK for the streams

The NATS operator's declarative `Stream` CRDs could replace the imperative stream Job that creates `LINEAGE`, `DLQ`
and `CATALOG_CONTROL` (Dapr's jetstream component does not auto-create streams). The Job works and is idempotent; NACK would remove a boot-ordering foot-gun, nothing more, and it would be
the platform's operator, not rask's.

## 4 · Do Dapr or Lance need an operator story? (asked 2026-07-11)

**Dapr — already covered, nothing to adopt.** Dapr's control plane *is* an operator
(`dapr-operator`, sidecar-injector, placement, sentry — helm-installed here, already running on
rask), and our pub/sub wiring is already declarative: `Component` CRs (`pubsub.jetstream`), a
`Subscription`-via-code model, and per-app scoping. The one imperative residue is stream
provisioning — that is the NACK row above, not a Dapr gap. Also pinned (python-infrastructure
skill + #115 review): **Dapr Workflow stays un-adopted** — every multi-step path we have
(bytes-then-commit registration) is token-keyed idempotent, so JetStream's
redeliver-the-whole-message model suffices; a workflow engine would add a sidecar state store
dependency for crash-recovery semantics we already get from idempotency keys.

> **Actors.** No service in the estate registers a Dapr actor, so placement holds nothing load-bearing; the one
> actor host, the notifications inbox, is retired ([ADR 0193](adr/0193-the-inbox-is-retired-and-the-console-joins-the-product.md)).
>
> **When to reopen this.** If a multi-step path appears whose steps cannot be made idempotent by any
> caller-chosen key — the honest signal is finding yourself wanting to *generate* an id mid-saga —
> the argument above stops applying and a workflow engine earns its dependency.
>
> **The lakehouse runs no ingest and hosts no workflow engine** ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md),
> [ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)): ingest and tier movement are bring-your-own
> client jobs that commit through the catalog. The criterion above is the question any new multi-step path is asked.

**Lance — no operator exists, none is missing.** Lance is a format + libraries; its "control
plane" is the manifest on S3, and CAS commits are the reconciler. The operator-shaped concerns it
DOES have are already owned elsewhere:

| Concern | Owner today | Status |
|---|---|---|
| background maintenance (version expiry, trash purge, orphan scan, reconcile, the due list) | `services/maintenance`: per-table passes on a NATS work queue, held after each commit and at least every `maintenance.queue.recheckHours`, plus the hourly audit and the daily scan (ADR 0198; FAIL-visible) | shipped; compaction and index builds are a bring-your-own job's, judged at `version/create` (ADR 0162) |
| stream/bucket/grant provisioning | chart Jobs + seed scripts (idempotent) | shipped; NACK could absorb the stream half |
| version/feature pinning (`pylance`, data_storage_version) | image pins + probe-before-callsite (§0) + `lance_docs/` | process, not software |

So: nothing to install for either.

## 5 · Secrets: the dev-mode fragility, and the operator plan (added 2026-07-14)

**What we hit.** Driving the #4 outbox live meant a helm upgrade that (via an unrelated interrupted
run) restarted the OpenBao pod. OpenBao runs `server -dev` → its KV store is **in memory**, so the
restart wiped `secret/lance`. The `openbao-seed` Job that repopulates it is a **post-upgrade hook**,
so it only fires on a helm release — not on a bare pod restart. Every new app pod's lifespan calls
`apply_dapr_secrets` → Dapr's vault secretstore → `GET /v1.0/secrets/lance-secrets/lance` → **`500`**;
the Dapr client retries with growing backoff, so uvicorn stays at "Waiting for application startup",
never binds `:8000`, and daprd sits "waiting for application to listen" — a two-sided deadlock that
reads like a crash but is a **missing secret**. Root-caused by running `apply_dapr_secrets` inside the
stuck pod under a watchdog; fixed by re-running the seed (the chart's own hook) + recreating the pod.

**Where it stands.** The seed runs beside the dev server and seeds an in-place restart again (§ 6,
`chart/templates/openbao-seed.yaml`), so a bare pod restart needs no release. The dev store stays in memory: it is
the openbao fixture, not for production. The chart renders no secret store ([[XC-149]]): production names the
platform's Vault or OpenBao, whose operator owns unsealing, durability and rotation, and the External Secrets
Operator is a prerequisite (docs/architecture/deployment.md, Prerequisite in production: the secret store).

## 6 · Event signing keys: provisioning, rotation and loss (LH-064)

Every signing identity (`lance.signingIdentities` in `chart/templates/_helpers.tpl`: the catalog and maintenance) signs the lineage it emits with its own Ed25519 key. The store
holds two secrets per identity, named by its key (the signer's ServiceAccount name, `lance.signingKeys`; the identity
itself is that account's subject, `kubernetes~system:serviceaccount:<ns>:<sa>`): `signing-public-<key>` (field `keys`: comma-separated public NKEYs, current
first, at most one previous) and `signing-key-<key>` (field `seed`: the NKEY user seed). A signer is Ready only
while the kid of its own seed is in its list, so the list is always written before the seed it belongs to. Lineage
reads lists only. The store enforces the same split: each sidecar logs in as its own ServiceAccount, and its role's
policy reads only its own app's secrets (XC-165, ADR 0169), so reaching :8200 without a sidecar's login reads nothing.

**The dev OpenBao (`openbao.enabled`) needs nothing from you.** Its `mint` init container generates a candidate
pair per identity on a memory volume the server never mounts, and its `seed` container chooses one pair per identity
(the kept copy, else the store behind the Service, else the candidate), keeps it, and writes the list first. A
rollout therefore carries every pair over; an in-place restart of either container writes the same pair again.

**The platform's store mints nothing.** The render refuses it until the secrets exist and
`--set signing.provisioned=true` attests it. Each identity needs `signing-public-<key>` (field `keys`: its public
NKEYs, comma-separated, current first) written before `signing-key-<key>` (field `seed`: its NKEY user seed);
`nk -gen user -pubout` prints a pair. Tooling for this is the production runbook, which waits for a production
estate (owner, no-prod parking 2026-09-21). Until the keys exist no signer is Ready and none emits: a sidecar
delivery to one is answered RETRY, not dropped.

**Retiring `service-token-<identity>` on the platform's store.** No door reads `secret/service-token-<identity>`
(a service is the ServiceAccount its projected token names), and no deploy removes one from a store the chart never wrote
to. Delete any that exist, one per identity (`bao kv metadata delete secret/service-token-<identity>`), and narrow the
`lance-infra` ESO policy to its one path, `secret/data/lance`.

**Rotation, at most once per 7 days** (the bus keeps 168 h and a retired key must stay listed until nothing signed
with it remains to verify). On the dev OpenBao, with its pod Ready (2/2) and not recently restarted, because the
replacement carries a pair over only from an outgoing pod that is serving a seeded store (LH-347):

```
kubectl exec deploy/rask-openbao -c seed -- bao kv metadata delete secret/signing-key-<key>
kubectl rollout restart deploy/rask-openbao
```

The seed in the replacement pod finds the list without its key, mints, prepends the new public key and keeps one
previous. A replacement pod starts with an empty kept copy, so the old key is not written back (an in-place restart of
the `seed` container would write it back from `/tmp/seed/signing-key-<key>`).

**The command is the metadata delete, never a soft `bao kv delete`.** Measured 2026-10-04 on openbao/openbao:2.2.0:
after `bao kv metadata delete`, `bao kv get -field=seed secret/signing-key-<key>` prints
`No value found at secret/data/signing-key-<key>`, which the seed reads as an absence and so rotates. After a
soft `bao kv delete` it prints `No data found at ...`, which the seed refuses as an unreadable store, so the
replacement pod never becomes Ready and the rollout wedges (the outgoing pod keeps serving, `maxUnavailable: 0`).

Rotation on the platform's store belongs to the parked production runbook. The signer re-resolves its key within
5 minutes and heals in place, and the retired key leaves the list at the next rotation.

**Loss.** A dev OpenBao replacement with no outgoing pod to carry from (a deleted pod, a drained node) mints every
identity afresh and the old public keys go with the old store. Events signed before the loss are refused once
enforcement is on (acked and counted); every signer re-resolves within 5 minutes once its sidecar can read again.

**Every replacement logs every sidecar out, a surge one included.** The store's tokens live in memory with it, so the
token each daprd got at boot through Kubernetes auth names nothing in the replacement, and every read through
`lance-secrets` answers 500 (`ERR_SECRET_GET`, the store's `403 permission denied`). Measured 2026-11-07 on
openbao/openbao:2.2.0 and daprd 1.19.0-rc.4: the fixture re-applied with a changed pod spec carried every signing pair
and bus user over a surge, and the catalog's, lineage's and maintenance's reads still failed three minutes later, until
`kubectl rollout restart deploy/rask-catalog deploy/rask-lineage deploy/rask-maintenance`; whether daprd's background
renewal would have logged in again later is not measured. So apply a fixture
change before the rask upgrade that rolls the Dapr pods, and restart every Dapr app after any other OpenBao replacement.

**Reading it back.** From an app's own sidecar, `GET /v1.0/secrets/lance-secrets/signing-key-<its key>` answers
200 with a `seed` field, every other identity's key is refused, and `signing-public-<key>` answers 200 to that signer and
to every verifier. Lineage's sidecar is refused every key. Each app's Dapr Configuration is named by the hash of its spec
(`lance-config-<app>-<hash>`) and its pods name it in `dapr.io/config`, so an allow-list edit renames the object and rolls
exactly the pods whose list changed. HotReload is off and Helm applies a Deployment before the Configuration it names:
a pod that boots first finds no such object, its daprd exits and the kubelet restarts it until Helm has applied it, so
no pod boots against the old list. During such an upgrade a rolled pod may show one daprd restart: that is the wait,
not a fault. Read a pod's Configuration from its `dapr.io/config`, not from the app-id.

## 7 · Bus credentials: one NATS user per app (XC-078)

The NATS bus is a prerequisite rask never installs (ADR 0153, XC-129); development runs it from `deploy/fixtures/nats`
(release `nats`, Service `rask-nats`). With the bus in operator mode (the fixture's `nats.auth.server`), a client connects
only with a user JWT signed under the operator's account, and each user may publish and subscribe only what its row of `nats.auth.users` (values.yaml)
grants. There is one user per Dapr app-id with a pub/sub component, plus `admin` (the stream Job), `monitor` (nats-box,
read-only: stream and consumer state, no messages). A grant only a flag's
component uses sits under `nats.auth.flagged` and is issued only while that flag is on. Each user's credential is
`nats-user-<user>` in the store (fields `jwt`, `seed`); every pub/sub Component names its own app's user by
`secretKeyRef` through `lance-secrets`, which the sidecar resolves with its own login, no app's Dapr Configuration lets
its code read a `nats-user-*`, and the stream Job (rask's release) and nats-box (the fixture's) take theirs as files written
by ExternalSecrets.

**Production (owner ruling R1): the platform runs NATS and its store; rask owns no trust root.** Name the bus in
`nats.url`, create `nats-user-<user>` for every user the chart's table issues, signed by the bus's operator; then
`--set nats.auth.provisioned=true`. Until then a render naming the platform's store (openbao.enabled=false) fails and
names every secret it needs. The server's own config and route credential are the platform's.

**The dev OpenBao mints the fixture bus's trust root.** It writes `nats-server` and `nats-route`, which the fixture's
ExternalSecrets read into the server's config through rask's SecretStore. Its seed carries `nats-root` (operator and
account keys) and `nats-route` from the outgoing pod over a surge rollout, keeps every declared user's key and credential under that root while its grants match (issuing a changed grant under the same key and revoking the old credential, and a new pair for a user whose stored seed is unreadable), revokes every user rask stops declaring, and writes the users before its readiness
key. A replacement with no outgoing pod mints a new root: a server in operator mode then refuses every client until the
NATS pods restart onto the new `nats-server`, and that restart starts an empty JetStream. Restart the NATS pods, rerun
the stream Job and restart every credentialed client after any non-surge OpenBao replacement.

**Switching it on (or after a trust-root change)** takes the bus down for a few minutes, because a reload cannot change
the operator or the system account and a pod restarted alone into operator mode cannot join the others: quiesce (outboxes at
depth 0); `kubectl scale statefulset/rask-nats --replicas=0` and wait until no NATS
pod exists; set `nats.auth.server: true` in `deploy/fixtures/nats/chart/values.yaml` and run `scripts/fixtures.sh nats`,
which restores the three replicas together (each waits for ESO's `rask-nats-auth`); then `make k3s-up`, whose stream Job
runs as `admin` and recreates the five streams empty; then `kubectl rollout restart` every Deployment with a credentialed
pub/sub component. Operator mode stays on only while git declares it: `scripts/fixtures.sh` installs with
`--reset-values`, so a `--set` given on one command line is dropped by the next install, which turns operator mode off and
restarts the bus into an empty JetStream as above.

**A consumer removal** (RESILIENCE.md gaps #7 and the lineage rebuild) needs `admin`, because nats-box runs as `monitor`.
Write a creds file into the nats-box pod from the store, without printing it:

```
JWT=$(kubectl exec deploy/rask-openbao -c seed -- bao kv get -field=jwt secret/nats-user-admin)
SEED=$(kubectl exec deploy/rask-openbao -c seed -- bao kv get -field=seed secret/nats-user-admin)
printf -- '-----BEGIN NATS USER JWT-----\n%s\n------END NATS USER JWT------\n\n-----BEGIN USER NKEY SEED-----\n%s\n------END USER NKEY SEED------\n' "$JWT" "$SEED" \
  | kubectl exec -i deploy/rask-nats-box -- sh -c 'umask 077; cat > /tmp/admin.creds'
kubectl exec deploy/rask-nats-box -- nats --no-context --server nats://rask-nats:4222 --creds /tmp/admin.creds consumer rm <STREAM> <durable> -f
kubectl exec deploy/rask-nats-box -- rm -f /tmp/admin.creds
```

**Reading it back.** On each node's monitoring port, `/connz?auth=true` lists every client connection with an
authorized user and none anonymous; `/connz?state=closed&auth=true` keeps short-lived ones. A publish
with no credential is refused ("Authorization Violation") and one with another app's credential on a subject that app
does not own is refused ("Permissions Violation"), each leaving the stream's last sequence unchanged.

**Residuals, named.** `_INBOX.>` is shared within the account, so a client could read another's replies (Dapr exposes no
inbox prefix). The catalog's grant on CATALOG_CONTROL_BROADCAST covers every consumer there, since each replica's
durable is named by its pod, so one replica could ack another's broadcast copy: the same identity, and the copies are
hints. Port 8222 is unauthenticated by design.

## 8 · Credentials: generated for the dev store, provisioned for a real one (XC-004)

No credential is a chart value (`templates/prod-credentials.yaml` refuses one by name), so none is in
`helm get values`, the manifest, a hook or a Job spec. The one exemption (owner, 2026-10-06) is the bundled dev Dex's
configuration (XC-025); the dev OpenBao's administrator token is generated in-cluster and mounted into its seed alone
(XC-165). A real-deployment render refuses both the bundled Dex and the dev OpenBao.

- **Dev (the openbao fixture).** Each generated credential is a Secret of its own, `<release>-dev-credential-<key>`
  for `object-store-secret-key`, `postgres-password` and `dapr-app-token` (`lance.devCredentials`). On a fresh install ESO's
  Password generator creates each once (`refreshPolicy: CreatedOnce`, `helm.sh/resource-policy: keep`), so a credential
  the chart adds later is generated on the next reconcile and the others never again. The OpenBao
  pod mounts them as one directory, and its seed writes the root to `secret/object-store-root` and each other generated field to its own
  secret (`postgres`, `dapr-app-token`) as `@file`. No workload's object-store identity is a secret: each signs with
  sessions its own ServiceAccount token is exchanged for (ADR 0197). The Secret outlives an
  OpenBao replacement, so the AGE database and the object store, which keep what they were initialised with, see
  the same values after a re-seed. The seed re-reads the mounted Secret every 5 s and re-seeds when it changes,
  so a rotation never needs the OpenBao pod replaced. The lakehouse uses no third-party credential; one a client
  uses (a model-download token, say) is that client's release's to provide (ADR 0189).
- **An estate whose stores already exist keeps its credentials.** If the AGE or object-store StatefulSet exists
  and the credential it holds (`postgres-password`, `object-store-secret-key`) has no Secret, the render refuses
  (`lookup`, so only against a cluster): generating beside live data would lock every consumer out of it. Create that
  credential's Secret, `<release>-dev-credential-<key>` with the one key `<key>`, from the value the estate holds first;
  a Secret that exists without an owner is operator-held and the chart generates nothing beside it.
- **Real deployment.** The platform's store holds every field docs/architecture/deployment.md lists, provisioned
  by its operator; the chart generates nothing.
- **Never delete the OpenBao pod to make it re-seed.** A replacement with no outgoing pod re-mints every signing
  pair (§ 6) and the NATS root (§ 7, an empty JetStream). The seed's loop is the re-seed path; a rollout restart
  (`kubectl rollout restart deploy/rask-openbao`, a surge) is the only safe replacement.
- **Rotating,** one class at a time, so a server learns a value only after the store and every ExternalSecret
  hold it and its consumers restart right after:
  1. Change the key of the credential's Secret, `<release>-dev-credential-<key>` (a real deployment: the field in the store), with a value
     generated in the cluster; wait for the seed's "the mounted credentials changed; seeding again". For
     `dapr-app-token`, delete its Secret instead and reconcile (`make k3s-up`): the chart's Password generator mints the
     new value in the cluster, so no person sees it.
  2. Force-sync every ExternalSecret that reads the store (`infra-credentials`, `object-store-root`, `observability-s3`,
     `dapr-app-token`) with the `force-sync`
     annotation, and wait for Ready. Their refresh is otherwise `externalSecrets.refreshInterval`, and a pod
     restarted before it takes the old value.
  3. Tell the server, when one holds the credential: `ALTER ROLE` inside AGE from stdin for `postgres-password`;
     restart `rask-rustfs` for `object-store-secret-key`. RustFS signs every STS session token with the root secret, so
     rotating root invalidates every outstanding session at once: every workload's own and every vended one. Clients
     re-vend, and a long write in flight fails.
  4. Restart the consumers at once: a `secretKeyRef` env and the injector's app token are read at pod creation,
     so nothing rolls on its own. AGE: OpenFGA (its migrate Job and the backup CronJob read it at their next run).
     The root: the catalog, lineage and maintenance, whose store sessions the old root signed and which exchange a new
     one at boot rather than at half a session's life. The app token: the sidecar Deployments (an app token rotated as
     below refuses no delivery in between).
  5. Read it back by refusal: one `ListBuckets` signed with the old root answers 403 `SignatureDoesNotMatch`, and the
     AGE role refuses the old password over the pod's own IP (`password authentication failed`). The image trusts
     loopback, so a login from 127.0.0.1 succeeds with any password and proves nothing. Measured 2026-11-07, rotating
     the three values XC-164's read-back exposed.
- **No workload's store identity needs rotation (ADR 0170, ADR 0197).** None holds a key pair: the catalog, lineage,
  maintenance and each backup CronJob project a token for `objectStore.identity.audience`, which the kubelet replaces
  before it expires, and exchange it through STS `AssumeRoleWithWebIdentity` for a session. A service holds an hour's
  session and exchanges again once half of it is used; a backup run asks for one at its start. A service whose session
  cannot be renewed logs `store_session_renewal_failed`, then `store_session_expired` once the session runs out, and
  answers 503 (code 17) until an exchange succeeds. What the store grants each is the canned policy named by its
  ServiceAccount's subject, `system:serviceaccount:<namespace>:<release>-sa-<component>`, as
  `<release>-object-store-identities` declares it: the provisioning hook writes it on the dev store, and a platform
  store's owner provisions it.
- **Rotating the Dapr app token, with no refused delivery (ADR 0166).** A door accepts the `token` and the `previous`
  field of the secret `dapr-app-token`, and daprd keeps the token its pod started with.
  1. `bao kv put secret/dapr-app-token previous=<the current token> token=<the new token>`, then
     wait ten minutes, so every door holds both before any sidecar can start with the new one: a daprd that
     restarts on its own after the sync would otherwise present a token its door does not hold yet.
  2. Force-sync the `<release>-dapr-app-token` ExternalSecret and wait for Ready.
  3. `kubectl rollout restart` every sidecar Deployment, so each daprd starts with the new token.
  4. Once every sidecar has restarted, clear the previous token (`bao kv patch secret/dapr-app-token previous=`).
     The doors stop accepting it within ten minutes; clearing it earlier refuses a sidecar still presenting it.
