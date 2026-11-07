# 0190. The notifications inbox is a client of the lakehouse's events (2026-11-02)

**Status:** accepted, amended by [ADR 0192](0192-a-services-roles-are-granted-at-the-catalogs-doors-scoped-to-the-tenants-it-serves.md) (`auth.externalDeciders` is gone: a client's roles are grants made at the catalog's access doors) and superseded in part by [ADR 0193](0193-the-inbox-is-retired-and-the-console-joins-the-product.md) (the inbox is retired and deleted, and the console joins the product; the contract below stays). Source: XC-188 (#755), admitted by the owner on 2026-11-01 through the multi-question tool ("Inbox
leaves, events stay"), narrowed on 2026-11-02 ("Lakehouse half only": the inbox's own release is client work) and
widened the same day ("Go": the lakehouse's own gates hold with every client tree deleted, checked on each push). Amends
[ADR 0189](0189-the-product-chart-carries-no-engine-seam.md), whose extension point `auth.externalDeciders` granted
each account it listed `decider` alone.

## Context

`services/notifications` keeps each person's inbox, read state, preferences, project watches and an email channel as
Dapr actor state. Two lakehouse feeds supply it: lineage's commit feed (the `lineage.events.v1` subscription and a cron
over `GET /events/projection`) and the catalog's control lane (`catalog.control.v1`). By the removal test it is a client:
deleting it changes no table, version, grant, lineage record or event. Read off the product chart on 2026-11-02, the
lakehouse carried it anyway:

- its Deployment, Service, ServiceAccount and the `/api/notifications` route, rendered by `fleet.yaml`, the last service
  that template rendered, with the fleet ConfigMap only it read;
- a NATS user, two durables, a control-lane Component, a cron Component, an SMTP Component, two Resiliency policies and
  its secret-store reads;
- the Dapr state store, scoped to it alone, with a database of its own in the AGE Postgres, its backup and its seeded
  DSN;
- the estate grants `decider`, `member_expander` and `event_reader`, the second and third named for it in a template;
- six alerts and a dashboard;
- Phase 1's criterion 4, which read its inboxes to call the events correct.

And `notifications` imported the event vocabularies it reads from `lakehouse_kit`, the kernel only the lakehouse runs.

Lakekeeper (`81d1b25`) emits a CloudEvent per change through its NATS and Kafka backends
(`crates/lakekeeper/src/service/events/publisher.rs`, `crates/lakekeeper-events-nats`, `crates/lakekeeper-events-kafka`)
and runs no inbox; a consumer reacts to the stream. Its one machine role is `server.operator`
(`authz/openfga/v4.13/components/server.fga`), granted at bootstrap or through its permissions API; its chart declares
no assignment. Polaris (`b1e9451`) hands each event to pluggable `PolarisEventListener`s, and its `sendNotification`
is a remote catalog telling it a table changed (`spec/polaris-catalog-apis/notifications-api.yaml`), not a message to a
person. The lance-namespace spec defines no event or notification surface (`lance_docs/ns_catalog/spec.yaml`), so the
contract the lakehouse's events carry is rask's own.

## Decision

- **The lakehouse emits; the inbox is a client.** The product chart renders no notifications object, and the render gate
  (`tests/unit/test_the_product_chart_renders_no_client.py`) refuses one. The inbox belongs with the console, as the web
  plane's per-user backend, and gets its own release as client work; until then the development estate runs no inbox.
- **The lakehouse keeps emitting lineage's feed and the control lane as they are, and their targeting fields are its
  published contract for any notifier** (`docs/COMMIT-EVENTS.md`): a run's verified `rask_author.sub`,
  `rask_lance.project`, `rask_lance.originator` and each output's `rask_lance.tableUuid`; a control event's
  `extra.subject`; `can_be_notified`, asked through the decision door; the whole run feed at `GET /events/projection`
  for an `event_reader`.
- **The contract a client reads the events by is the platform's**: `service_kit.control_events`,
  `service_kit.lineage_events`, `service_kit.governed.subjects` and `service_kit.signature_doors`. The kernel keeps the
  emitters. A client imports nothing from `lakehouse_kit`; the inbox's own import contract says so.
- **One generic external grant.** `auth.externalDeciders` maps each `<namespace>:<serviceaccount>` outside the release to
  the estate roles it holds, from `decider` (the decision door's check), `member_expander` (its list-users over a
  membership userset) and `event_reader` (the whole run feed). bootstrap-admin grants each account exactly its roles,
  and revokes each machine role on the estate root (those three and lineage's `governance_prober`) that neither the map
  nor lineage's own grant declares, so an account removed from the map keeps nothing. The model's one role per route
  stays: no account holds a role it was not named for.
- **The lakehouse's gates hold with every client tree deleted.** No client is a root package of `.importlinter`, which
  forbids each as an external name; `make lint-imports` runs every member's own contract it finds; `dagger call
  lakehouse-alone` deletes every client tree, re-locks the workspace and runs the contracts and the offline suite, on
  every push.

## Consequences

Read against the estate ADR 0193 leaves, where the inbox is retired and deleted and the console ships in the product
chart:

- The product chart renders no notifications object and no state store, so the AGE Postgres serves OpenFGA's database
  alone. A live estate keeps the `daprstate` database and its rows, since an init script drops nothing; nothing reads
  them.
- The lakehouse keeps emitting lineage's feed and the control lane, and their targeting fields stay its published
  contract (`docs/COMMIT-EVENTS.md` § Telling people), with `can_be_notified` asked through the decision door and the
  whole run feed at `GET /events/projection` for an `event_reader`. No notifier runs on the estate: one is a client a
  client's repository builds on this contract.
- A notifier's roles are grants to its own identity made at the catalog's access doors (ADR 0192), never a value.
- The NATS stream Job deletes every `-durable` on a stream it walks that no component in the release renders, so a
  durable a client creates on `lineage.events.v1` or `catalog.control.v1` does not outlive the next upgrade; the topics
  are internal, and a notifier that must miss no run walks the feed over HTTP.
- No store role the product chart renders names a client (`lance.storeReads`). A client's release provisions its own
  secret-store reads through the `openbao.externalReaders` seam, and its bus user through `nats.auth.users`, both empty
  in the product's values.
- Criterion 4 claims what the lakehouse emits (each grant names the person it concerns, each run names its tables by
  uuid), not who was told.
- A bus user the chart stops declaring is revoked: the dev store's seed revokes it in the APP account by its key and
  deletes its secret, and revokes a replaced credential at its issue time (ADR 0193). On a platform's NATS, the operator
  that provisions the users revokes one the chart stops declaring.
- service-kit carries the contract a client reads the events by; a client imports nothing from `lakehouse_kit`, and the
  lakehouse's gates hold with every client tree deleted.
