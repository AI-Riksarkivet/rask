# Operators: what the platform runs, and what rask's own would reconcile

Which Kubernetes operators sit around rask, who installs them, and the operational procedures rask itself owns
(secrets, signing keys, NATS users, credentials, § 5 onward).

## 1 · Prerequisites are the platform's

The product chart installs rask and nothing else ([ADR 0153](adr/0153-the-app-chart-is-the-product.md)). Every
operator and control plane rask relies on is a prerequisite the platform runs, named by reference; development
installs each from `deploy/fixtures`, its own Helm release, before rask's (`make k3s-up`;
[architecture/deployment.md](architecture/deployment.md)):

| Prerequisite | What rask renders against it |
|---|---|
| **External Secrets Operator** | the ExternalSecrets and SecretStore behind every credential a pod takes as a Kubernetes Secret (§ 8) |
| **NATS JetStream** | the stream Job that declares rask's streams and durable consumers, and one bus user per app (§ 7) |
| **Postgres** | OpenFGA's config file (`<release>-infra-credentials`) names it, only while the chart runs OpenFGA: `postgres.externalHost`, or in development the `postgres` fixture (`postgres.enabled`) |
| **The OTLP gateway** | `observability.export.endpoint` and `observability.identity.audience`: each service exports to the gateway as itself with its ServiceAccount token ([ADR 0215](adr/0215-each-service-exports-its-telemetry-to-the-platforms-gateway-as-itself.md)); the gateway, store, dashboards and alerting are the platform's |

**Two things the platform's edge owes the lakehouse**, since rask runs neither a gateway nor a telemetry store:

- **A traffic policy on the Gateway that serves the chart's HTTPRoutes.** No lakehouse service limits a caller's
  request rate (the catalog sheds only concurrent writes). The door to limit first is the catalog's decision door,
  `POST /management/v1/decisions/check` under the catalog's public prefix: one question names up to 1,000 objects and
  costs about 40 OpenFGA batch requests, and any signed-in caller may ask. A per-client limit of a few requests a
  second there, and a looser one on the rest of `/api/*`, is the platform's to set and tune; rask has measured none.
- **A count of the audit records the OTLP gateway admits**, as the metric `audit.records.admitted`
  ([architecture/deployment.md](architecture/deployment.md), "The trail's records are counted"). The rules in
  `deploy/alerting/rules.yml` compare it with what the services count, so a trail that loses records pages somebody.

The lakehouse runs no compute and submits no job ([ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)):
KubeRay, Kueue, GPU operators and whatever orchestrates a pipeline belong to the user who runs that pipeline, and no
lakehouse service imports `ray` (the import contract `the-lakehouse-knows-no-client`).

## 2 · The catalog converges what git declares; an operator comes later

The tenants an estate holds are declared in git and converged by the catalog itself
([ADR 0206](adr/0206-the-tenant-stays-above-the-warehouse-names-belong-to-the-warehouse-and-the-catalog-converges-what-git-declares.md),
XC-213), so an install from git holds its projects, warehouses and service grants with no call to any door. Argo CD
applies the chart; the catalog reconciles what the chart declares.

**What is declared, and where.** The chart's `catalog.declared` values hold three lists:

- `projects`: `{id, admins, protected}`. `admins` are people's principals (`<idp-id>~<user id>`); a project naming none
  is administered by `auth.bootstrapAdmin` when that is set. The default values declare one project, `default`.
- `warehouses`: `{id, project, bucket, delete_profile, protected, active}`. `bucket` defaults to the id; the id is the
  first segment of every name in the warehouse (`<warehouse>$<namespace>…$<table>`).
- `grants`: `{object, relation, principal}` on `project:<id>` or `warehouse:<id>`, to a principal that is not a person
  (a service account, `kubernetes~system:serviceaccount:<namespace>:<name>`). A person's grant is refused in the
  declaration; it is made at the catalog's access doors.

The chart adds the estate's own grants to the same file ([[XC-011]]): `estate.admins`, from `auth.bootstrapAdmin`
(`admin` on `estate:rask` and `owner` of the estate's own warehouse, `warehouse:lance_catalog`), and `estate.roles`, the
machine roles the release's services hold on the estate root (`lance.fgaEstateRoles`). Changing `auth.bootstrapAdmin`
moves both tuples to the new principal at the next tick, and the audit trail names each revoke and grant
(`access_tuple_delete`, `access_tuple_write`, subject `system:declaration`). No hook writes OpenFGA.

Namespaces, tables and people's grants other than a project's admins and the estate's are never declared: they stay with
their doors.
The chart renders the lists into the ConfigMap `<release>-catalog-declaration` (key `declaration.json`), which the
catalog mounts as a volume at `/etc/rask/declaration` (`LANCE_DECLARATION_FILE`), so a change reaches a running pod with
no roll.

**The tick.** The catalog's fixed timer `catalog-declaration-converge` (`catalog.declarationConverge.periodSeconds`,
every 60 s by default) runs in every replica and one replica claims each window on the control root
(`lakehouse.timers`); that replica reads the mounted file and converges onto it in order: projects, then
warehouses, then what git stopped declaring, then grants (`catalog/api/declaration_converge.py`). It makes what is
missing and corrects what drifted (a warehouse's bucket, delete profile, status and protection; a project's
protection) through the same functions the doors call, so each change is audited and announced as a door's is, its
actor `system:declaration`. One object's failure leaves every other object to that tick, and the next tick tries again.
A file that cannot be read as a declaration converges nothing and releases nothing.

**`managed_by: git` and the lock.** Each declared project and warehouse carries `managed_by: git` on its record. A
door asked to change what git declares of it answers 403 code 15 naming the declaration, audited as a denial with reason
`managed_by_git`: for a warehouse, a re-`POST /management/v1/warehouses` of its id, `deactivate`, `activate`,
`protection` and `DELETE`; for a project, `protection` and `DELETE`. The lock is asked after the door's own
authorization, so only a caller who could otherwise make the change learns that git owns it, and it holds on a stack
that authenticates nobody. Grants, namespaces and tables beneath a declared object stay with their doors. A warehouse is
marked only once the catalog has established it to the end (its record, its row in its own catalog, its grants), so
one whose establish failed part-way is established again by the next tick and is not locked meanwhile.

**A warehouse does not move between projects.** A declaration naming a standing warehouse under another project than
its record's is refused for that warehouse on every tick (`warehouse:<id>` under `failed`), and its status, protection
and mark stay as they are: correct the declaration, or delete the warehouse and declare it anew.

**Removal from git.** A project or warehouse removed from the lists is released: its `managed_by` clears, its data and
records stay, and its doors change it again. A declared grant is recorded under `_declared_grants/` on the control root
before its tuple is written, and only a recorded tuple is revoked once git stops declaring it; a tuple the store already
held when the declaration asked for it is not recorded, so a grant made at a door is never taken. A project's declared
admin is revoked through the members door's own rule, which keeps a project its last administrator: that revoke is
refused, and counted as a failure on each tick, until another admin is granted.

**Reading whether it converged.** Until the operator exists there is no per-object status in Kubernetes; read the
catalog:

- the log line `declaration_converged` (logged when a tick changed something or failed), whose `converged`,
  `released`, `grants_written`, `grants_revoked` and `failed` name what it did; `declaration_converge_failed` (WARNING)
  for each object a tick could not converge, and `declaration_unreadable` (ERROR) for a file it could not read;
- the counter `catalog.declaration_converge.failed`, labelled `lance.catalog.object` (`project:<id>`, `warehouse:<id>`,
  `grant:…`, `revoke:…`, `declaration`);
- `managed_by` on `GET /management/v1/projects/{id}` and `GET /management/v1/warehouses/{id}` (`"git"`, or `"self"` for one its own doors manage), which
  the console shows as "Declared in git".

**Procedure.** Each step is a change to the values in git, applied by Argo CD (or `make k3s-up` in development); the
catalog converges it within a tick.

1. Declare a project, a warehouse and a service grant:

   ```yaml
   catalog:
     declared:
       projects:
         - id: acme
           admins: ["dex~alice"]
       warehouses:
         - id: acme-bronze
           project: acme
           delete_profile: {mode: soft, grace_days: 7}
       grants:
         ingest-writes-bronze:
           object: warehouse:acme-bronze
           relation: writer
           principal: "kubernetes~system:serviceaccount:pipelines:ingest"
   ```

   Read it back: `GET /management/v1/warehouses/acme-bronze` answers `managed_by: "git"`, and the catalog's log holds a
   `declaration_converged` line naming `project:acme` and `warehouse:acme-bronze` with `grants_written` counting the
   admin and the service grant. Names in the warehouse begin with its id: `POST /v1/namespace/acme-bronze$sales/create`.
2. Change one: set `active: false` (or `protected: true`) on the warehouse in git. The next tick deactivates it; a
   `POST /management/v1/warehouses/acme-bronze/activate` at the door is refused 403 code 15 while git declares it.
3. Remove one: delete the grant from `grants`, and the next tick revokes that tuple alone. Delete the warehouse from
   `warehouses`, and the next tick releases it (`released` names it): its tables stay, and its own doors (including
   `DELETE`) apply to it again.

**The operator comes later** (XC-210, ADR 0206 § 5) and is thinner than first planned: it would write the same
declaration from custom resources and add a status per object, which today is read from the catalog's log and doors.
It is not a prerequisite for GitOps. The state of record stays where it is: catalog and lakehouse state, lineage's
commit feed and edge list included, lives in **Lance manifests on S3**, and authorization in **OpenFGA**; the
declaration is intent, never a copy of either. The chart's provisioning Jobs (the object store's policies, the bus
permissions, the signing pairs) stay idempotent Jobs for the development fixtures and the platform's own controllers in
production (§ 3 for the streams): reconciling them continuously would mean holding the platform store's admin
credential, the bus account's signing key and write access to the secret store, which zero trust refuses. The
reference operators' source was read on 2026-11-10
([architecture/operator-findings.md](architecture/operator-findings.md)).

## 3 · Optional: NACK for the streams

The NATS operator's declarative `Stream` and `Consumer` CRDs could replace the stream Job that declares `LINEAGE`, `DLQ`
and `CATALOG_CONTROL` and the durable consumers (no service creates a stream or a consumer). The Job converges each
object to its declared config on every reconcile and retires the consumers git no longer declares; NACK would remove a
boot-ordering foot-gun, nothing more, and it would be the platform's operator, not rask's.

## 4 · Does Lance need an operator story? (asked 2026-07-11)

**No workflow engine.** Every multi-step path the lakehouse has (bytes-then-commit registration) is token-keyed
idempotent, so JetStream's redeliver-the-whole-message model suffices; a workflow engine would add a state store
dependency for crash-recovery semantics the idempotency keys already give.

> **When to reopen this.** If a multi-step path appears whose steps cannot be made idempotent by any
> caller-chosen key — the honest signal is finding yourself wanting to *generate* an id mid-saga —
> the argument above stops applying and a workflow engine earns its dependency.
>
> **The lakehouse runs no ingest and hosts no workflow engine** ([ADR 0154](adr/0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md),
> [ADR 0155](adr/0155-the-medallion-is-tier-governance-in-the-catalog.md)): ingest and data movement are bring-your-own
> client jobs that commit through the catalog. The criterion above is the question any new multi-step path is asked.

**Lance — no operator exists, none is missing.** Lance is a format + libraries; its "control
plane" is the manifest on S3, and CAS commits are the reconciler. The operator-shaped concerns it
DOES have are already owned elsewhere:

| Concern | Owner today | Status |
|---|---|---|
| background maintenance (version expiry, trash purge, orphan scan, reconcile, the due list) | `services/maintenance`: per-table passes on a NATS work queue, held after each commit and at least every `maintenance.queue.recheckHours`, plus the hourly audit and the daily scan (ADR 0198; FAIL-visible) | shipped; compaction and index builds are a bring-your-own job's, judged at `version/create` (ADR 0162). On an estate with no such job, `make compact-due` runs the same caller by hand, as the person whose bearer is in `COMPACT_CATALOG_TOKEN` (`scripts/compact_due_tables.py`) |
| stream/bucket/grant provisioning | chart Jobs + seed scripts (idempotent) | shipped; NACK could absorb the stream half |
| version/feature pinning (`pylance`, data_storage_version) | image pins + probe-before-callsite (§0) + `lance_docs/` | process, not software |

So: nothing to install.

## 5 · Secrets: the dev-mode fragility, and the operator plan (added 2026-07-14)

**What we hit (2026-07-14).** Driving the #4 outbox live meant a helm upgrade that (via an unrelated interrupted
run) restarted the OpenBao pod. OpenBao then ran `server -dev`, its KV store **in memory**, so the restart
wiped every secret, and the seed that repopulated it was a **post-upgrade hook**, which fires on a helm
release and not on a bare pod restart. Every app's start then found its secrets missing and retried with growing
backoff: uvicorn stayed at "Waiting for application startup" and never bound `:8000`, a wait that read like a crash
and was a **missing secret**. Fixed by re-running the seed and recreating the pod.

**Where it stands.** The seed runs beside the dev server and seeds an in-place restart again (§ 6,
`chart/templates/openbao-seed.yaml`), so a bare pod restart needs no release. The dev store stays in memory: it is
the openbao fixture, not for production. Each service logs in to the store itself before it serves ([[XC-199]]), asks
again for about two minutes, and then fails its start naming its role, so an empty or unreachable store shows as a
start that failed on its login. The chart renders
no secret store ([[XC-149]]): production names the
platform's Vault or OpenBao, whose operator owns unsealing, durability and rotation, and the External Secrets
Operator is a prerequisite (docs/architecture/deployment.md, Prerequisite in production: the secret store).

## 6 · Event signing keys: provisioning, rotation and loss (LH-064)

Every signing identity (`lance.signingIdentities` in `chart/templates/_helpers.tpl`: the catalog and maintenance) signs the lineage it emits with its own Ed25519 key. The store
holds two secrets per identity, named by its key (the signer's ServiceAccount name, `lance.signingKeys`; the identity
itself is that account's subject, `kubernetes~system:serviceaccount:<ns>:<sa>`): `signing-public-<key>` (field `keys`: comma-separated public NKEYs, current
first, at most one previous) and `signing-key-<key>` (field `seed`: the NKEY user seed). A signer is Ready only
while the kid of its own seed is in its list, so the list is always written before the seed it belongs to. Lineage
reads lists only. The store enforces the same split: each service logs in as its own ServiceAccount,
and its role's policy reads only its own app's secrets (XC-165, ADR 0169, XC-199), so reaching :8200 without that login
reads nothing.

**The dev OpenBao (`openbao.enabled`) needs nothing from you.** Its `mint` init container generates a candidate
pair per identity on a memory volume the server never mounts, and its `seed` container chooses one pair per identity
(the kept copy, else the store behind the Service, else the candidate), keeps it, and writes the list first. A
rollout therefore carries every pair over; an in-place restart of either container writes the same pair again.

**The platform's store mints nothing.** The render refuses it until the secrets exist and
`--set signing.provisioned=true` attests it. Each identity needs `signing-public-<key>` (field `keys`: its public
NKEYs, comma-separated, current first) written before `signing-key-<key>` (field `seed`: its NKEY user seed);
`nk -gen user -pubout` prints a pair. Tooling for this is the production runbook, which waits for a production
estate (owner, no-prod parking 2026-09-21). Until the keys exist no signer is Ready and none emits.

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
enforcement is on (acked and counted); every signer re-resolves within 5 minutes once the store admits its login again.

**Every replacement invalidates every store token, a surge one included.** The store's tokens live in memory with it, so
the token each service holds names nothing in the replacement. A service reads under its own login
(XC-199), and its loop has that token ask the store about itself every 15 s (`auth/token/lookup-self`, which the
rendered policy grants): the replacement answers `403 permission denied`, the pass logs `secret_store_token_dropped`
and logs in again, and reads are served again within one pass of the replacement admitting the login. Until then a
read that reaches the store (a holder reading its key again, as the signing key's does every 300 s) is
refused and logged `secret_store_read_failed`, and readiness reports `secret-store: unavailable` as `degraded`, which
keeps the pod in rotation (`lakehouse.governed.secrets`; held offline by
`packages/lakehouse/tests/test_a_process_reads_its_secrets_from_the_store_as_itself.py`). Measured 2026-11-14 on helm
rev 8 (openbao/openbao:2.2.0; `kubectl rollout restart deploy/rask-openbao` at 10:33:00 UTC): the
replacement was Ready 21 s later with every signing pair and bus user carried (18 of the store's 20 secrets
byte-identical, the other two the seed's own marker and the re-packed archive of the same bus root), each of the six
lakehouse pods logged `secret_store_token_dropped` and `secret_store_logged_in` on its first pass after that (1 to 6 s
later, 22 to 27 s after the restart), no pod restarted or left Ready, the catalog's `/readyz` answered `degraded` for
5 s, and no read was refused in the five minutes watched: the store's replacement alone restarts no pod.

During a surge two stores answer at the Service's address, and each refuses the other's tokens. A service whose pass
logs in to one can then be refused, by the other, the question about the token it has just been issued: its loop logs
`secret_store_token_cannot_ask_about_itself` once (the same line a role whose policy lost the
`auth/token/lookup-self` grant produces) and logs in again at its next pass, so a login that landed on the store that
then left is replaced within one more pass. Each further login the store refuses in turn waits twice the passes the
last did (1, 3, 7, then 15, four minutes), and the store's first answer ends that. The drill of 2026-11-14 did not
meet this case: the outgoing pod left the Service as the replacement joined it, and each service logged in once. A
replacement that mints a new bus root still needs the NATS pods restarted and the stream Job run again (§ 7).

**Reading it back.** Logged in to the store as an app's own account and role (a token from
`kubectl create token <release>-sa-<app> --audience rask-openbao`, presented at `auth/kubernetes/login` as
`<release>-<app>`), `signing-key-<its key>` reads with a `seed` field, every other identity's key is refused 403, and
`signing-public-<key>` reads for that signer and for every verifier; lineage's role is refused every private key.
`tests/e2e-py/test_each_app_reads_only_its_own_secrets.py` asks exactly this of a deployed estate.

## 7 · Bus users: a session per pod, no stored credential (XC-078, XC-199)

The NATS bus is a prerequisite rask never installs (ADR 0153, XC-129); development runs it from `deploy/fixtures/nats`
(release `nats`, Service `rask-nats`). It runs in operator mode, and each connection may publish and subscribe only what
its user's row of `nats.auth.users` (values.yaml) grants. There is one user per connection a service opens: `catalog` and
`lineage` each publish and take their deliveries on a connection of their own as that user (`lakehouse.events.bus`), whose
replies arrive under `_INBOX_<user>`, a prefix the table grants to that user alone; `maintenance-worker` works the queue
and pulls the write-event lane the same way; and `maintenance` exists only while the worker publishes
(`maintenance.lineageEmit`, `maintenance.controlEmit`). Each service binds to the durable consumer the stream Job
declares for it and may create, edit or delete none. Beside them are `admin` (the stream Job) and `monitor` (nats-box,
read-only: stream and consumer state, no messages). A grant only a flag's call site uses sits under `nats.auth.flagged`
and is issued only while that flag is on.

**A session user holds no credential.** A row with a ServiceAccount is a session user: the lakehouse's own rows take
theirs from the chart (`catalog` -> `<release>-sa-catalog`, `lineage` -> `<release>-sa-lineage`, `maintenance` and
`maintenance-worker` -> `<release>-sa-maintenance`, `admin` -> `<release>-sa-jobs`), and a row supplied by values names
its own with `serviceAccount` (a ServiceAccount of the release's namespace, never one the lakehouse renders or
`default`). Its pod projects the ServiceAccount token for the audience `nats.auth.audience` (default `rask-bus`, 600 s,
at `/var/run/secrets/rask/identity/<audience>/token`, `lance.busIdentity`) and presents it as the connection's token,
under the user's name, over TLS. The bus hands the connection to its signer, which checks the token with a TokenReview
for that audience and answers with a session as that user that ends within `nats.auth.sessionSeconds` (default 300,
at least 60 and below 600), signed by that user's scoped signing key; the bus enforces the key's scope, which is the
user's row. The client asks for a new session shortly before the old one ends, on the same connection (`lakehouse.events.bus`;
the maintenance worker's queue does the same in `maintenance.services.queue`). No `nats-user-<user>` secret exists for a
session user: none is minted, read, mounted or attested. The pod is given `RASK_BUS_URL`, `RASK_BUS_USER`,
`RASK_BUS_TOKEN_FILE`, `RASK_BUS_SESSION_SECONDS` and, with `nats.ca.configMap`, `RASK_BUS_CA_FILE` (`lance.busEnv`).
The stream Job runs `nats` with `NATS_TOKEN` read from the projected token file on every call and
`--connection-name admin`.

**The signer's table.** The chart renders every session user's ServiceAccount, publish and subscribe lists, with the
audience and `sessionSeconds`, as ConfigMap `<release>-nats-permissions` (key `permissions.json`,
`templates/nats-permissions.yaml`) in every mode. The signer picks the user whose ServiceAccount equals the token's and
whose name equals the connection's name, and refuses a connection whose name matches none.

**A static user is a row with no ServiceAccount** (`monitor`, `object-store`, a client's row without `serviceAccount`):
its credential is `nats-user-<user>` in the store (fields `jwt`, `seed`), read by a development fixture (nats-box's
`monitor` and the object store's `object-store` creds are files written by ExternalSecrets) or by a client under its own
store login (`openbao.externalReaders`). No lakehouse pod is one.

**TLS is required on a real deployment.** Each pod's token crosses in `CONNECT`, so a render on a real deployment
refuses a `nats.url` whose scheme is not `tls://` (`templates/prod-credentials.yaml`). `nats.ca.configMap` (key
`nats.ca.key`, default `ca.crt`) names a ConfigMap of the release's namespace holding the CA the bus's certificate
chains to; it is mounted read-only into every session user's pod. Empty means the system trust store.

**A `tls://` server takes the TLS handshake before its greeting** (nats-server `tls { handshake_first: true }`, or
`auto` to serve clients of both kinds; the NATS fixture sets `auto`). Every client of the release opens with the
handshake (`lakehouse.events.bus`, the maintenance worker's queue, the stream Job's `--tlsfirst`), because a NATS client that
waits for the greeting encrypts only when that unencrypted line asks it to: a server that leaves the request out, or
anyone on the path who removes it, is then sent the pod's token in the clear (measured on nats-py 2.16.0, 2026-11-15,
`packages/lakehouse/tests/test_a_process_publishes_to_the_bus_as_itself.py`). Against a server that greets first the
handshake fails and nothing is sent: the pods stay unconnected and say `bus_not_connected`.

**Production (owner ruling R1): the platform runs NATS, its TLS, its signer and its store; rask owns no trust root.**
Name the bus in `nats.url` (`tls://`), its CA in `nats.ca`, and have the signer read ConfigMap
`<release>-nats-permissions`: for every session user it admits a token of that ServiceAccount for the audience, only
under that user's name, with a session of at most `sessionSeconds` signed by a signing key scoped to the user's row; a
changed row changes that key's scope. Create `nats-user-<user>` for each static user the table lists, signed by the bus's
operator. Then `--set nats.auth.provisioned=true`. Until then a render naming the platform's store
(openbao.enabled=false) fails and names every session user and static user it needs. The server's own config, operator
and route credential are the platform's.

**The dev OpenBao mints the fixture bus's trust root.** It writes `nats-server` (operator, system account,
`resolver_preload`, the `default_sentinel`), `nats-route` and `nats-callout`, which the fixture's ExternalSecrets read into
the server's config and the stand-in signer through rask's SecretStore. The trust root `nats-root` holds the operator, the
SYS, APP and AUTH accounts and their keys. In APP the seed converges one scoped signing key per session user (role: the
user's name; scope: its row), re-scoping the same key when a row changes and removing the key of a user that is no
session user, which closes that user's sessions. In AUTH it keeps the signer's user `callout`, a bearer `sentinel` denied
every subject (the server's `default_sentinel`, so a pod presents no NATS JWT), one signing key for the signer's answers,
and the callout naming `callout` and APP. It issues each static user's JWT and seed as `nats-user-<user>`, keeping a
user's key and credential while its grants match (issuing a changed grant under the same key and revoking the old
credential, and a new pair for a user whose stored seed is unreadable), revokes every static user rask stops issuing, and
writes the bundle before its readiness key. A replacement with no outgoing pod mints a new root: a server in operator
mode then refuses every client until the NATS pods restart onto the new `nats-server`, and that restart starts an empty
JetStream. After any non-surge OpenBao replacement, restart the NATS pods (the signer reads its keys from `nats-callout`
through its own ExternalSecret) and rerun the stream Job; each service's own connection opens again by itself.

**The dev estate runs the bus like a platform's.** `make k3s-up` installs the NATS fixture with `nats.auth.server=true`
(`k3s-fixtures` in the Makefile): operator mode, TLS on the client port from a CA and certificate the fixture makes once
and keeps (Secret `rask-nats-tls`; the CA alone in ConfigMap `rask-nats-ca`, which `deploy/values-local.yaml` names in
`nats.ca.configMap` beside `nats.url: tls://rask-nats:4222`), and the stand-in signer `rask-nats-signer`
(`deploy/fixtures/nats/callout`, two replicas, bound to `system:auth-delegator` for the TokenReview). The development
object store publishes its events over the same CA (`events.busCa`, `deploy/fixtures/rustfs`) as the static user
`object-store`. The stand-in logs one line per decision (user, ServiceAccount, granted or the reason it refused) and never
a token.

**Switching operator mode on (or after a trust-root change)** takes the bus down for a few minutes, because a reload
cannot change the operator or the system account and a pod restarted alone into operator mode cannot join the others:
quiesce (outboxes at depth 0); `kubectl scale statefulset/rask-nats --replicas=0` and wait until no NATS pod exists; run
`make k3s-up`, whose `k3s-fixtures` installs the fixture with `nats.auth.server=true` and restores the three replicas
together (each waits for ESO's `rask-nats-auth`) and whose stream Job runs as `admin` and recreates the streams and
their durable consumers empty; each service's own connection opens again by itself, and its consumer is pulled again once
the stream Job has declared it. ON AN ESTATE WHOSE STORE HOLDS NO SIGNER YET (the first switch), the fixture's servers
and signer wait for secrets that rask's own seed mints, so the order is: scale the bus to 0; install the fixture in
operator mode without waiting on it (`scripts/fixtures.sh nats -- --set nats.auth.server=true`, which makes the CA and
the TLS Secret at once and leaves the pods waiting for `rask-nats-auth`); upgrade rask, whose seed mints AUTH, the
sentinel, the signer's user and each session user's key in place; then have ESO read them
(`kubectl annotate externalsecret rask-nats-auth rask-nats-signer rask-nats-signer-keys force-sync=$(date +%s) --overwrite`),
after which the three servers and the signer start and the stream Job (a post-upgrade hook) declares the streams.
Measured on the dev estate, 2026-11-15, helm revisions 13 and 14: the bus was away 23 minutes, most of it a signer that
could not read its own credential (497acf5d1); no service pod restarted, and each connected within a minute of the
signer's first answer. Operator mode stays on only while git declares it: `scripts/fixtures.sh nats` run by hand
installs with `--reset-values`, so a `--set` given on one command line is dropped by the next install, which turns
operator mode off and restarts the bus into an empty JetStream as above.

**A reload of the bus's config ends every session, and each is opened again at once.** When the server's config
changes (a changed row re-scopes a key, so the APP account's JWT in `resolver_preload` changes and the reloader signals
the servers), the server asks its signer about each open connection again, without the token that connection presented;
the signer refuses (`no token presented`) and the server closes the connection. A service opens a new one in-process the
same second (`bus_connection_ended`, then `bus_connected`); a maintenance worker ends its process and starts again (ADR
0198). Measured on the dev estate, 2026-11-15 10:30:59 and 10:31:06 UTC, nats-server 2.15.2: every session user refused
once per reload and connected again inside the second. ESO refreshes the fixture's Secrets hourly, so a changed row
reaches the bus and the signer within the hour, or at once with the `force-sync` annotation above.

**Revoking a service** is a change of the table: remove its row, or its `serviceAccount`, and the signer's key for it goes
at the next reconcile (the dev seed removes the key of a user that is no session user), which closes every session that
key signed; a new connect is refused. A deleted pod's token fails its TokenReview, so its next connect is refused, and a
session it holds ends within `sessionSeconds`.

**A consumer removal** (RESILIENCE.md gap #3, the lineage rebuild) needs `admin`, because nats-box runs as `monitor`.
Ask the bus for a session as `admin` the way the stream Job does, with a token for its ServiceAccount, without printing
it:

```
kubectl get configmap rask-nats-ca -o jsonpath='{.data.ca\.crt}' | kubectl exec -i deploy/rask-nats-box -- sh -c 'umask 077; cat > /tmp/ca.crt'
kubectl create token <release>-sa-jobs --audience rask-bus --duration 10m \
  | kubectl exec -i deploy/rask-nats-box -- sh -c 'NATS_TOKEN=$(cat) nats --no-context --server tls://rask-nats:4222 --connection-name admin --tlsfirst --tlsca /tmp/ca.crt consumer rm <STREAM> <consumer> -f'
kubectl exec deploy/rask-nats-box -- rm -f /tmp/ca.crt
```

**Reading it back.** On each node's monitoring port, `/connz?auth=true` lists every client connection with an
authorized user and none anonymous; `/connz?state=closed&auth=true` keeps short-lived ones; each connection's `name` is
its bus user. The signer's log (`kubectl logs deploy/rask-nats-signer`) names each decision: the user, its ServiceAccount
and granted, or the reason it refused. A connection with no token, or whose name matches no session user of the token's
ServiceAccount, is refused and leaves the stream's last sequence unchanged; a publish on a subject outside the user's row
is refused ("Permissions Violation") and the connection is kept.

**Residuals, named.** `admin`, `monitor` and the development object store receive their replies on the shared
`_INBOX.>`, so within the account each of the three could read the replies to another (answers about streams and
consumers, never a service's, which arrive under an inbox of its own). A publisher's read-back grant
(`$JS.API.STREAM.MSG.GET.<stream>`, with which it checks the stored copy a duplicate acknowledgement names) reads any
message of the stream it publishes into, since the bus scopes that call by stream and no finer. No service's user may
create a consumer (each catalog replica reads CATALOG_CONTROL by sequence instead): the bus holds a consumer's deliver
subject to no grant of its creator, so a create grant would let its holder have the bus copy a stream's messages onto a
subject it may not publish. Measured on nats-server 2.15.2, 2026-11-14: under a create grant on CATALOG_CONTROL, a
consumer delivering to `store.events` was created, and STORE_EVENTS kept its delivery. Port 8222 is
unauthenticated by design.

## 8 · Credentials: generated for the dev store, provisioned for a real one (XC-004)

No credential is a chart value (`templates/prod-credentials.yaml` refuses one by name), so none is in
`helm get values`, the manifest, a hook or a Job spec. The one exemption (owner, 2026-10-06) is the bundled dev Dex's
configuration (XC-025); the dev OpenBao's administrator token is generated in-cluster and mounted into its seed alone
(XC-165). A real-deployment render refuses both the bundled Dex and the dev OpenBao.

- **Dev (the openbao fixture).** Each generated credential is a Secret of its own, `<release>-dev-credential-<key>`
  for `object-store-secret-key` and `postgres-password` (`lance.devCredentials`). On a fresh install ESO's
  Password generator creates each once (`refreshPolicy: CreatedOnce`, `helm.sh/resource-policy: keep`), so a credential
  the chart adds later is generated on the next reconcile and the others never again. The OpenBao
  pod mounts them as one directory, and its seed writes the root to `secret/object-store-root` and the other generated field to its own
  secret (`postgres`) as `@file`. No workload's object-store identity is a secret: each signs with
  sessions its own ServiceAccount token is exchanged for (ADR 0197). The Secret outlives an
  OpenBao replacement, so the development Postgres and the object store, which keep what they were initialised with, see
  the same values after a re-seed. The seed re-reads the mounted Secret every 5 s and re-seeds when it changes,
  so a rotation never needs the OpenBao pod replaced. The lakehouse uses no third-party credential; one a client
  uses (a model-download token, say) is that client's release's to provide (ADR 0189).
- **An estate whose stores already exist keeps its credentials.** If the development Postgres's or the object store's StatefulSet exists
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
     generated in the cluster; wait for the seed's "the mounted credentials changed; seeding again".
  2. Force-sync every ExternalSecret that reads the store (`infra-credentials`, `object-store-root`, `observability-s3`)
     with the `force-sync`
     annotation, and wait for Ready. Their refresh is otherwise `externalSecrets.refreshInterval`, and a pod
     restarted before it takes the old value.
  3. Tell the server, when one holds the credential: `ALTER ROLE` inside the development Postgres (`rask-postgres`) from stdin for
     `postgres-password`;
     restart `rask-rustfs` for `object-store-secret-key`. RustFS signs every STS session token with the root secret, so
     rotating root invalidates every outstanding session at once: every workload's own and every vended one. Clients
     re-vend, and a long write in flight fails.
  4. Restart the consumers at once: OpenFGA reads its mounted config file at start, so nothing rolls on its own. Postgres: OpenFGA (its migrate Job reads it at its next run).
     The root: the catalog, lineage and maintenance, whose store sessions the old root signed and which exchange a new
     one at boot rather than at half a session's life.
  5. Read it back by refusal: one `ListBuckets` signed with the old root answers 403 `SignatureDoesNotMatch`, and the
     Postgres role refuses the old password over the pod's own IP (`password authentication failed`). The image trusts
     loopback, so a login from 127.0.0.1 succeeds with any password and proves nothing. Measured 2026-11-07, rotating
     the three values XC-164's read-back exposed.
- **No workload's store identity needs rotation (ADR 0170, ADR 0197).** None holds a key pair: the catalog, lineage,
  maintenance and the control-root backup CronJob project a token for `objectStore.identity.audience`, which the kubelet replaces
  before it expires, and exchange it through STS `AssumeRoleWithWebIdentity` for a session. A service holds an hour's
  session and exchanges again once half of it is used; a backup run asks for one at its start. A service whose session
  cannot be renewed logs `store_session_renewal_failed`, then `store_session_expired` once the session runs out, and
  answers 503 (code 17) until an exchange succeeds. What the store grants each is the canned policy named by its
  ServiceAccount's subject, `system:serviceaccount:<namespace>:<release>-sa-<component>`, as
  `<release>-object-store-identities` declares it: the provisioning hook writes it on the dev store, and a platform
  store's owner provisions it.
