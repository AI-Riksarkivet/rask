# 0228. The chart runs no Postgres (2026-11-23)

**Status:** accepted. Source: [[LH-496]] (#640), decided by the owner on 2026-11-23 ("Dump and restore into a new
fixture", "Remove it", and "The database is managed by the plattform team.. why do we need to care about backup?").
Supersedes ADR 0125 (AGE on CloudNativePG behind its own gate) and the Postgres half of ADR 0153's "the lakehouse's own
stores are optional subcharts"; ADR 0033's pg_dump fallback is withdrawn.

## Context

The product chart rendered a Postgres StatefulSet, `rask-age`, on the `apache/age` image, on by default. It served one
thing, OpenFGA's database. Nothing used the AGE extension: lineage keeps no database (ADR 0158). Around it the chart
and the repository carried an off-by-default AGE-on-CloudNativePG `Cluster`, an extension image build, a CloudNativePG
operator fixture with a 1.2 MB CRD bundle, a nightly `pg_dump` CronJob with its own store identity, and a helm wrapper
that probed that server for a SQL release store.

A Postgres is a platform service. The platform team runs it, makes it highly available and backs it up. In production
rask's services do not open a database connection at all: the catalog talks to OpenFGA's API, and the production
overlay uses the platform's OpenFGA.

How the references package it: Lakekeeper's chart bundles Postgres as an optional subchart its README calls not
production ready, and points production at an external database. Polaris's chart bundles none and gives development
throwaway databases from fixtures. rask follows the second shape, as it already does for its secret store, object
store, bus and identity provider.

## Decision

1. **The product chart renders no Postgres, no database backup and no CloudNativePG object.** `age-postgres.yaml`,
   `age-cluster.yaml`, `backup-pg.yaml`, the `age.*` values, `backups.pgDump` and `security.infraContexts` are gone.
2. **The chart names a Postgres only while it runs OpenFGA itself.** `postgres.externalHost` is the platform's;
   `postgres.enabled` says the estate uses the development fixture. The ExternalSecret assembles OpenFGA's connection
   string from that host and the store's `postgres` secret. With the platform's OpenFGA (`openfga.enabled=false`,
   `auth.fgaApiUrl`) the render names no Postgres and no value under `postgres` is read.
3. **Development and CI get a stock Postgres from a fixture**, `deploy/fixtures/postgres`: one server, one volume, one
   database, its image named by digest, its password the dev store's. It has no backup, and a real deployment that
   runs the chart's OpenFGA is refused it, as it is refused the other development stand-ins.
4. **rask backs up no database.** The grants live in OpenFGA's database, whose backup and restore are its owner's. The
   control-root backup stays: the control root is rask's own state on the object store.
5. **rask has no CloudNativePG prerequisite.** The operator fixture, its CRD bundle, `make k3s-crds` and the extension
   image build are gone.

## The four facts

- **Cloud native:** met. The chart names its platform services and bundles none.
- **GitOps:** met. The fixture is rendered from git like the others; no value lives only on a release. The dev
  estate's existing grants moved once, by hand, from the old volume to the fixture's; an install from git needs no
  such step.
- **Zero trust:** improved. One identity fewer holds the database password (the backup job's), and its store role and
  ServiceAccount are gone.
- **Lance-namespace/table-idiomatic:** not involved.

## Consequences

- A deployment that runs the chart's OpenFGA on a platform Postgres whose backups are poor has no second copy from
  rask. That is the platform's to fix.
- The development Postgres is not durable. Losing its volume loses the dev estate's door-made grants; the catalog's
  declaration converges the declared ones again.
- The helm wrapper holds no release-store logic. Releases live in Helm's Secret store, which the chart fits at about a
  third of its cap.
