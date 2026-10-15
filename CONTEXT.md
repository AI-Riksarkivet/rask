# rask

An agnostic, multimodal lakehouse: Lance tables governed by a catalog, with lineage, authorization, events and
annotation across them. Every write, from any engine the user brings, comes in through the catalog's Lance
Namespace doors; the lakehouse runs no compute and no workflow engine of its own.

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
A throwaway install of a prerequisite for development or CI, never for production and never part of the product chart.
_Avoid_: dev stack, bundled copy, stand-in

**Engine**:
The user's bring-your-own compute and orchestrator, which runs their pipeline and writes into the lakehouse through
the catalog's namespace doors like any client; the lakehouse never calls it and knows nothing of how it runs.
_Avoid_: Ray, compute plane, cluster

### Governance

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
