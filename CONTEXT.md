# rask

An agnostic, multimodal lakehouse: Lance tables governed by a catalog, with lineage, authorization and events across
them. Every write, from any engine or tool the user brings, comes in through the catalog's Lance Namespace doors; the
lakehouse runs no compute, no workflow engine and no annotation tool of its own.

## Language

### Deployment

**Product chart**:
The Helm chart that installs rask itself and nothing else: its services, zones and their configuration.
_Avoid_: umbrella chart, platform chart, the chart

**Prerequisite**:
A service rask needs that someone other than rask installs and runs, named by reference: Dapr, NATS JetStream, an OTLP
endpoint, an object store and an OIDC issuer.
_Avoid_: dependency, bundled service, platform component

**Optional subchart**:
One of the lakehouse's own stores (OpenFGA, the Postgres behind AGE and OpenFGA) that the product chart can install for
development and that production replaces with an external instance.
_Avoid_: embedded database, internal service

**Fixture**:
A throwaway install, for development or CI and never for production, of a prerequisite or of a client the lakehouse is
developed against; never part of the product chart.
_Avoid_: dev stack, bundled copy, stand-in

**Client**:
Anything that reaches the lakehouse only through its public doors: the catalog's namespace and management doors, the
credentials it vends, lineage's event feed, and the identity provider. A user's pipeline on their engine is a client,
and so is an annotation tool, rask's annotator and Label Studio alike. The lakehouse never calls a client and holds none
of its state, vocabulary or authorization.
_Avoid_: integration, plugin, consumer service

**Engine**:
The user's bring-your-own compute and orchestrator, which runs their pipeline and writes into the lakehouse through
the catalog's namespace doors like any client; the lakehouse never calls it and knows nothing of how it runs.
_Avoid_: Ray, compute plane, cluster

### Catalog

**Name**:
The path of segments a client uses to reach a table or namespace, which the Lance spec calls its `id`: `["bronze",
"räksmörgås"]`, written `bronze$räksmörgås`. A rename changes it. Two names are the same name only when they are equal
exactly, case included, and a name not in Unicode NFC is refused rather than rewritten. Each segment is 1 to 255
characters holding no `$`, `+`, `/` or control character and no leading or trailing whitespace; a bare `+` in a
request's path reads as a space.
_Avoid_: id, object id, identifier (for the minted key)

**Table uuid**:
The key the catalog mints for a table when it is created, declared or registered, and that every grant and record about
the table names. It never changes: a rename and an undrop keep it, while a table re-created under a dropped name gets a
new one.
_Avoid_: table id, object id, key

**Namespace uuid**:
The key the catalog mints for a namespace when it is created, under the same rules as a table uuid.
_Avoid_: namespace id, object id, key

**Location**:
Where a table's bytes live. A table the catalog makes lives at `<warehouse root>/<table uuid>`, so no name is ever part
of one; a registered table keeps the location its registrar named.
_Avoid_: path, directory, prefix, table root

**Location claim**:
The catalog's record of which table holds a location, and of that table's uuid. One table holds a location at a time: a
rename hands the claim to the new name, and destroying the table releases it.
_Avoid_: lock, lease, ownership record

### Governance

**Principal**:
Who a verified caller is, `<idp-id>~<subject>`: the identity provider that vouched for them and the subject it names. A
service is `kubernetes~system:serviceaccount:<ns>:<sa>`; a person is the idp id the deployment gives their Dex connector
and the user id that connector reports, so renaming a connector re-keys no grant. Every grant, inbox and attribution
names a principal, and a bare name, which no token can prove, is refused wherever one is required.
_Avoid_: sub, subject id, user name, OIDC sub

**Tier**:
One of the governed stages of a table's data: bronze, silver or gold. Raw is the external world, never a tier. A
table's tier is the one its nearest enclosing namespace declares, and a namespace declares it once, when it is created,
by a project admin (the estate admin where no project owns the namespace). A name never says what tier something is.
_Avoid_: layer, zone, stage

**Tier judge**:
The one decision every write door in the catalog asks before it writes: may this operation, on this table, by this
caller, under this run, proceed. It answers allow or refuse, and it owns the caller's write permission as well as the
tier's rules, so no write door decides on its own.
_Avoid_: tier guard, write gate

**Tier governance**:
The rules the catalog applies in its own write path to every write into a tier, so no writer can bypass them: the
writer's permission on the tier's namespace, and on silver and gold the writer's run and inputs (ADR 0156; the run-and-inputs rule is LH-225).
_Avoid_: the medallion service, the cascade

**Pipeline**:
The user's job that reads one tier and writes the next through the catalog, on their own engine.
_Avoid_: stage runner, cascade

**Commit announcement**:
The event the catalog records for every committed write, read by a pipeline from lineage's durable `GET /events` feed
with its own token. The NATS topics it travels on are internal.
_Avoid_: cascade head, bronze arrival

**Promotion gate**:
The check a project declares for its tables, run when a version is published by moving the table's `published` tag,
the version readers of a governed tier read. With review declared, a version that breaches it is held, not published.
_Avoid_: promotion saga, review workflow

**Hold**:
A version the promotion gate kept from readers for a person to decide: approving it publishes it, rejecting it returns
the table to its published version, and a hold nobody decides in time expires with nothing published.
_Avoid_: pending promotion, review

**Erasure obligation**:
A data subject's erasure the catalog has accepted for one table, open until the catalog has itself read that the
subject is gone from everything the table's owner can rewrite. The owner's own job does the rewriting; the catalog
never does it, never retries it, and shows the obligation overdue once its deadline passes.
_Avoid_: erasure job, erasure task, GDPR request
