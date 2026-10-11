# rask

An agnostic, multimodal lakehouse: Lance tables governed by a catalog, with lineage, authorization, events and
annotation across them, running batch work on an engine it does not own.

## Language

### Deployment

**Product chart**:
The Helm chart that installs rask itself and nothing else: its services, zones and their configuration.
_Avoid_: umbrella chart, platform chart, the chart

**Prerequisite**:
A service rask needs that someone other than rask installs and runs, named by reference: Dapr, NATS JetStream, an OTLP
endpoint, an object store, an OIDC issuer, and the compute engine.
_Avoid_: dependency, bundled service, platform component

**Optional subchart**:
One of the lakehouse's own stores (OpenFGA, the Postgres behind AGE and OpenFGA) that the product chart can install for
development and that production replaces with an external instance.
_Avoid_: embedded database, internal service

**Fixture**:
A throwaway install of a prerequisite for development or CI, never for production and never part of the product chart.
_Avoid_: dev stack, bundled copy, stand-in

**Engine**:
The bring-your-own compute that runs a workload's batch jobs; the lakehouse reaches it through a configured endpoint and
knows nothing of how it runs.
_Avoid_: Ray, compute plane, cluster
