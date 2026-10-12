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
One of the governed stages of a table's data: bronze, silver or gold. Raw is the external world, never a tier.
_Avoid_: layer, zone, stage

**Tier governance**:
The rules the catalog applies in its own write path to every write into a tier, so no writer can bypass them: the
tier's permission, and on silver and gold the writer's run and inputs (ADR 0156; the run-and-inputs rule is LH-225).
_Avoid_: the medallion service, the cascade

**Pipeline**:
The user's job that reads one tier and writes the next through the catalog, on their own engine.
_Avoid_: stage runner, cascade

**Commit announcement**:
The event the catalog records for every committed write, read by a pipeline from lineage's durable `GET /events` feed
with its own token. The NATS topics it travels on are internal.
_Avoid_: cascade head, bronze arrival

**Promotion gate**:
The check a project declares for a tier, run when a version is published; readers of a governed tier read the version
tagged `published`.
_Avoid_: promotion saga, review workflow
