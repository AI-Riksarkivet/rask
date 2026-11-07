# 0193. The inbox is retired, and the console joins the product (2026-11-06)

**Status:** accepted. Source: XC-195 (#773). The owner ruled "Console in product" on 2026-11-06 (the console belongs to
the lakehouse, with its own settings and its own login) and, for the notifications inbox, "Drop it, like all three" the
same day. On 2026-11-07 the owner folded two more decisions into the row: the dev store's seed revokes what the
release stops declaring (a zero-trust fix, pre-approved by the project rule), and "Delete them in XC-195" for
service-kit's client app factory and actor-route guard. Supersedes in part [ADR 0190](0190-the-notifications-inbox-is-a-client-of-the-lakehouses-events.md): the
inbox it describes is retired, and its contract stays. Spec #778 (the inbox leaving for a repository of its own) is
closed by this ruling.

## Context

Measured 2026-11-06 at main `0faa294c3` by the parity audit against Lakekeeper `9124ea8`, whose gap 3 is client
admissions rendered by the lakehouse release:

- **The product had no UI.** `chart/templates` held no console template. The console shipped only as the development
  release `deploy/fixtures/console`, and its sign-in client `rask-console` was declared in that release's
  `dex-values.yaml`, layered onto Dex by a Makefile glob.
- **That release also deployed the notifications inbox** (`services/notifications`: one Dapr `InboxActor` per subject on
  a CloudNativePG state store, an email channel through the `mailpit` fixture, two verify scripts in `tests/e2e`), and
  through `deploy/fixtures/console/lakehouse-values.yaml` it made the lakehouse release render the inbox's estate roles,
  its NATS bus user and its secret-store reads.
- **A bus user the release stopped declaring stayed valid.** The dev store's seed issued every user of the table and
  stopped writing one rask no longer named, but a user JWT carries no expiry and the APP account that signed it stays
  trusted, so the credential kept admitting whoever held it, and its secret stayed readable. The store's Kubernetes-auth
  roles and policies were written from the rendered documents and never pruned, so an identity git dropped kept logging
  in. Measured offline 2026-11-07 on nats-server 2.14.2 with nsc 2.15.0, MEMORY resolver: a key revoked in the APP
  account is refused once the account JWT reaches `resolver_preload` and the server reloads its config (SIGHUP); a client
  connected as that user is dropped at the reload, the other users keep connecting, and a restart keeps the revocation.
- **service-kit's client app factory ran nowhere.** `make_service_app` and its actor-route guard `guard_actor_routes`,
  with what only they used (service-kit's `Settings`, `register_middleware`, `setup_otel`, the slash tolerance, the
  health router, the actor state-store probe and warm-up), were the inbox's boot; with the inbox gone, nothing in the
  repository ran them. The lakehouse builds from `build_lance_service_app` in its own kernel.

The reference catalogs, read from source the same day:

- **Lakekeeper ships its UI with the product.** The console is a dependency of the server binary
  (`crates/lakekeeper-bin/Cargo.toml:21,37`, the `ui` feature and `lakekeeper-console`), served at `/ui`
  (`crates/lakekeeper-bin/src/ui.rs:221-224`) and configured from the server's own config (`ui.rs:29-97`: the issuer,
  the client id, the scopes).
- **Lakekeeper runs no inbox.** It emits CloudEvents for every change (`README.md:51`, "Change Events"), and a consumer
  reacts to the stream. Its console's bell lists only the results of the reader's own actions, kept in the browser:
  a Pinia store of `{id, function, text, type, timestamp, read}`, newest first, at most 100, entries older than 24 h
  pruned (`lakekeeper/console-components` v0.33.0 `af70695`, `src/stores/notifications.ts`), fed when an API call's
  caller records its success or failure (`src/plugins/functions.ts:273-470`, `handleSuccess`, `handleError`,
  `setError`). The store is persisted with `pinia-plugin-persistedstate-2` under the key `notifications`.
- **Polaris** (`a10e2df`) hands each event to pluggable listeners (`polaris.event-listener.types`, `CHANGELOG.md:715`).
  Its "notifications" are catalog-to-catalog table-metadata sync (`spec/polaris-catalog-apis/notifications-api.yaml`),
  not a message to a person.
- **Gravitino** (`463296c`) loads event-listener plugins (`EventListenerPlugin`, `gravitino.eventListener.names`,
  `docs/gravitino-server-config.md:604-610`). It runs no inbox.
- **`lance_docs`**: the namespace spec defines no event or notification surface (`ns_catalog/spec.yaml`), so how a
  person is told is the implementation's.

## Decision

- **The product chart renders the console** (`chart/templates/console.yaml`, `console.enabled`, default on): the
  Deployment and Service `rask-console` serving the `web-console` image, pinned by the chart's own image helper; the
  ServiceAccount `rask-sa-console` with no token mounted, since the console calls no door as itself, holds no grant and
  joins no list of accounts the catalog or the bootstrap grants; the ConfigMap `rask-console-config` holding the
  `/config.json` the browser boots from; and the HTTPRoute `rask-console`, `PathPrefix /` on `route.parentRefs`. The
  `/api/*` routes and `/dex` are longer prefixes and keep winning. The pod runs non-root on a read-only root filesystem
  with every capability dropped, and under `networkPolicy.enabled` admits its port from anywhere and dials nothing.
- **The console's sign-in is the lakehouse's own.** `/config.json` is `auth.oidc.issuer`, `console.oidc.clientId`
  (`rask-console`) and `console.oidc.scopes` with `federated:id` and the audience scope added by the chart, since every
  governed door requires both, so no value can drop them; with `auth.enabled=false` it is `{"signIn": false}`. The
  development Dex declares the public client under its own `console` value (PKCE S256, no secret, a trusted peer of the
  audience client, its dev origin answered for CORS), apart from the `clients` seam so no client's values can replace
  it.
- **The bell is a browser-side log of the reader's own writes.** Every write the console declares records its outcome,
  a success or a failure with its HTTP status and `detail`; reads are not logged, since a read's failure renders on its
  own page. The log is kept in `sessionStorage` under the signed-in principal (in memory where storage is unavailable),
  at most 100 entries and none older than 24 h, and is cleared at sign-out. The pages `/notifications`, `/preferences`
  and `/preferences/notifications` go.
- **The inbox is retired and deleted**, with everything only it used: `services/notifications` and its image, its
  Dapr components and actor state store, its admissions in the lakehouse release, the `mailpit` fixture, `tests/e2e`,
  `@rask/api`'s inbox client, the console release `deploy/fixtures/console`, `make dev-micro` and `make k3s-clients`.
  Git keeps every one.
- **The lakehouse keeps emitting its signed events and keeps its targeting contract** for any notifier a client builds
  (`docs/COMMIT-EVENTS.md` § Telling people, ADR 0190's contract, service-kit's `control_events`, `lineage_events`,
  `governed.subjects` and `signature_doors`, the `can_be_notified` verb and the roles the decision door answers).
- **The dev store's seed revokes what the release stops declaring.** Before it issues the bus table, the seed revokes
  in the APP account every `nats-user-*` the release no longer declares, by the key in the JWT that secret stores (read
  from its own store or the one behind the Service), and then deletes that secret from its own store. After writing the
  rendered roles and policies it deletes every Kubernetes-auth role and policy no rendered document names, keeping the
  store's `default` and `root`. A declared user keeps its key pair and JWT across reseeds while its grants match; a
  changed grant is reissued under the same key and the JWT it replaces revoked `--at` its issue time; a stored user whose
  seed is unreadable is issued a fresh pair and its old key revoked, so the store heals without a hand-run delete; and the APP
  account's `*` revocation sits one second before the oldest JWT a declared user holds, so whatever earlier reseeds left
  behind is refused too. What git stops declaring stops admitting anyone, on the next seed, with no hand-run step.
- **service-kit ships no app factory** (owner, 2026-11-07, "Delete them in XC-195"). `make_service_app`,
  `guard_actor_routes` and everything only they used are deleted. A client is an outside tool on the public API and
  builds its own app, as a Lakekeeper client does; service-kit keeps the contract a client reads the events by, with
  `control_signer_role` naming the role whose signature a notifier requires.
- **This repository hosts no client.** The console is the lakehouse's own UI, so the no-client lint
  (`scripts/lakehouse_names_no_client.py`) scopes the console's tree too, and `notifications` and `mailpit` join its
  departed names.

## Considered options

- **Move the inbox to a repository of its own** (spec #778): rejected by the owner's ruling. None of the reference
  catalogs runs one, and the lakehouse's events and published contract already serve any notifier a client builds.
- **Keep the console as a development release beside the product:** rejected by the owner's ruling. An install of the
  product would have no UI, and Lakekeeper ships its console with its server.
- **Keep the bell's log in `localStorage`, as Lakekeeper's persisted store does:** rejected. It outlives the tab and the
  sign-out, so the next person on the same browser profile would find the previous person's actions. `sessionStorage`
  under the principal, cleared at sign-out, keeps nothing of a person past the tab, as the tokens held in memory do.
- **Keep `make_service_app` as a platform seam for clients:** rejected by the owner's ruling. A factory no code here runs
  is tested by nothing that ships, and a client building on it would couple its boot to the lakehouse's tag.
- **Log reads beside writes:** not taken. A read's failure is shown on the page that asked, and a log of every read would
  bury the writes the reader wants to find.

## Consequences

- An install from git serves the console at `/` with the lakehouse; `make k3s-up` installs both, and
  `make k3s-build`/`make k3s-import` build and load `web-console` with the lakehouse image.
- The bell shows nothing another person did and nothing the lakehouse did on its own: a run that wrote a table the
  reader may read, or a grant made to them, reaches them only through a notifier a client builds on the contract.
- The development Dex holds the console's client whatever clients a client's values add.
- The lakehouse release renders no notifications account, bus user, durable, Component or secret reader; the
  `openbao.externalReaders`, `networkPolicy.objectStoreExtraFrom` and `nats.auth.users` seams stay, empty.
- A reseed with unchanged grants changes no credential a running sidecar holds. A client holding a revoked JWT is
  closed after two refused reconnects (measured 2026-11-07, nats-server 2.14.2, nsc 2.15.0, nats.go 1.39.1), the closed
  connection `service_kit.bus_watchdog` restarts its sidecar from.
- A revoked bus user stays refused across reseeds and broker restarts; the revocation reaches a running bus at its next
  config reload, which the bus fixture's reloader sends when its auth Secret changes, and drops a client still connected
  as that user. A platform's NATS and secret store are their operator's: the operator revokes what the chart stops
  declaring, as ADR 0190 says.
- service-kit no longer carries `Settings`, `register_middleware`, `setup_otel` or the health router; a client that used
  them builds its own. Its pytest plugin and the event contract stay.
- Removed on the live estate at deploy: the release `console-notifications` and the Postgres
  `Cluster/rask-notifications-state` holding the inbox's actor state. Deleting the cluster is destructive, so it is
  asked of the owner first.
