# 0207. The lakehouse runs no Dapr, and each pod asks the bus for a session with its own token (2026-11-15)

**Status:** accepted. Source: the owner, 2026-11-08, "move away from Dapr after LH-494" (#638); the plan of
[ADR 0199](0199-nats-is-the-one-bus-and-the-design-review-reopens-eight-decisions.md) § Consequences, its track D, worked
as nine steps of XC-199 (#787) from 2026-11-13 to 2026-11-15, each through `/implement #787`, each deployed and read back
on the development estate; the owner's choices through the multi-question tool on the way ("Fresh session" for D4, "You
are allowed to do d6", "Switch dev now" for D7). This record is that track's D8. Supersedes the credential mechanism of
[ADR 0086](0086-nats-authenticates-every-client-one-user-per-app-xc-078.md) for the lakehouse's own pods; amends
[ADR 0198](0198-maintenance-is-driven-by-commits-and-a-clone-keeps-its-source-by-a-tag.md) decision 5 and
[ADR 0166](0166-a-secret-read-from-the-store-is-re-read-on-a-ttl.md); closes ADR 0199's track D.

## Context

Until 2026-11-14 every lakehouse Deployment ran a Dapr sidecar for three jobs: publishing and subscribing on NATS
JetStream (five Components), reading secrets from the store (one Component), and six fixed timers (`bindings.cron`). Each
carried a measured defect or a workaround (the audit's inventory, #787's body): a JetStream outage of about two minutes
closed every Component for good, so each service ran a watchdog that restarted its sidecar; a cron binding fired on every
replica; the sidecar could not change a durable it held; every Component edit rolled the pods; a production platform
had to run Dapr's control plane for it. Two credentials never expired: each NATS user's JWT and seed, and one app token
shared by every Dapr-delivered route. The fastapi reference justifies Dapr only for Dapr Workflow, which the lakehouse
forbids ([ADR 0154](0154-the-lakehouse-runs-no-ingest-and-no-workflow-engine.md)).

## Decision

1. **No lakehouse pod runs a sidecar, the product chart renders no `dapr.io` object, annotation or label in any mode,
   the repository installs no Dapr control plane, and no lakehouse package imports `dapr`.** Held by ruff (`TID251`),
   import-linter (`.importlinter`), the render test
   (`tests/unit/test_invariants.py::test_no_lakehouse_pod_runs_a_sidecar_and_the_chart_renders_no_dapr_object`) and a
   lint over every other code file (`scripts/the_lakehouse_runs_no_dapr.py`, in `make lint` and `dagger call lint`).
   Where each need is met instead is the `rask-dapr` skill's table.
2. **The bus is each process's own connection** (`lakehouse.bus`, nats-py). Each event goes out once under its own
   message id inside a structured CloudEvent the lakehouse builds (`lakehouse.cloud_events`); a duplicate acknowledgement
   counts only once the stored copy is read back and carries the same event. Deliveries are pulled from durable consumers
   the chart's stream Job declares (`Bus.consume`) and settled by one rule (`lakehouse.bus_delivery.judged`: taken, asked
   again after a delay, or parked by the lane), and each catalog replica follows the control topic by reading the stream
   by sequence (`Bus.follow`), with no consumer on the bus.
3. **A pod holds no bus credential.** Each connection presents the pod's projected ServiceAccount token (audience
   `nats.auth.audience`) under its bus user's name, and opens with the TLS handshake before the server's greeting. The
   bus's signer, a NATS auth callout (ADR-26 of nats-architecture-and-design), checks the token with a TokenReview and
   grants a session of at most `nats.auth.sessionSeconds` (300), signed by that user's scoped account signing key, so the
   server enforces the user's row of `nats.auth.users` whatever the signer writes. The client asks for its session again
   shortly before it ends, on the same connection, and opens a new one in-process when the bus ends one. The chart renders
   the session users' table as ConfigMap `<release>-nats-permissions` for the signer, and refuses a real deployment whose
   `nats.url` is not `tls://`. The bus, its TLS and its signer are the platform's; the development estate runs a stand-in
   (`deploy/fixtures/nats`, `callout/`). Users that are no lakehouse pod's (the fixtures' `monitor` and `object-store`, a
   client's row without `serviceAccount`) stay static users with a stored credential.
4. **Each service logs in to the secret store itself** (`lakehouse.governed.secrets`): Kubernetes auth as its own role
   `<release>-<app>`, with its own projected token, the login made again inside its lease and never renewed.
5. **A fixed estate timer is a loop of its own service that claims each window on the store** (`lakehouse.timers`: one
   conditional write of `_timers/<timer>.json`, the store's clock naming the window), so it runs once a window whatever
   the replica count and depends on nothing its work does not need. A time per object (a table's next pass, a commit's
   follow-up) stays a NATS message schedule (ADR 0198 decision 6). A new time-shaped need is one of the two.

## Amendments

- **ADR 0198 decision 5.** Maintenance's worker is no longer the one service on the bus directly: every service is.
  Its queue connection presents its pod's token as `maintenance-worker` and renews its session on the same client, so a
  pass carries across a renewal. It says a bus outage once and keeps reconnecting, and still ends its process when the
  bus ends its session, as before.
- **ADR 0166.** The shared secret cache it decided has no implementation: each holder of a secret re-reads it on its own
  period under its own login (decision 4). The two credentials it served without expiry are gone: no lakehouse pod
  holds a bus seed (decision 3), and the app token went with the routes it guarded.
- **ADR 0086.** One user per app and the permission table stand; for the lakehouse's own pods the user is a session the
  signer grants, not a JWT and seed the store holds.

## Consequences

- **The stream Job carries no migration.** A consumer that starts by stream sequence and does not exist yet begins at
  the stream's first message, and the retire phase removes only consumers that carry the release's mark; the steps that
  carried the push durables' positions over and removed them by name served one deploy and are gone.
- **The pods' grace period stays 40 s.** It was set as the preStop sleep (5 s), uvicorn's drain (25 s) and the
  lifespan's teardown (10 s, `lifecycle` in `chart/values.yaml`); the teardown now also closes the bus connection, whose
  close is bounded by two waits of 3 s (`lakehouse.bus._CLOSE_SECONDS`). Nothing in it was the sidecar's.
- **A reload of the bus's config ends every session.** The server asks its signer about each open connection again,
  without the token it presented, and closes it on the refusal; each service opens another in the same second and a
  maintenance worker starts again (measured on the development estate, 2026-11-15, nats-server 2.15.2;
  `docs/OPERATORS.md` § 7). Whether a platform's signer can answer such a re-check was not tried.
- **A `tls://` bus must serve the TLS handshake first** (`handshake_first`). nats-py encrypts only when the server's
  unencrypted greeting asks for it, whatever the address says, so a server that leaves the request out would be sent the
  pod's token in the clear (measured on nats-py 2.16.0, 2026-11-15); against a server that greets first, nothing is sent.

## Evidence

Each step's close-out comment on #787 holds its read-back, and the measurements behind decision 3 are
`docs/architecture/bus-credentials-findings.md`. On 2026-11-15 the development estate ran helm revision 14 with no Dapr
object in the cluster; no lakehouse pod mounted a Secret, took an environment variable from one, or held an NKEY seed or a
NATS user JWT; the signer granted every lakehouse user through a real TokenReview and refused a token of another
ServiceAccount, of another audience and none. CI on `0c17d8dbf` passed every lane but the dependency audit, which failed
before this track.
