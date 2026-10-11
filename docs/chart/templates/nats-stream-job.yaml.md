# chart/templates/nats-stream-job.yaml

Rationale for `chart/templates/nats-stream-job.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

Create the JetStream LINEAGE stream (subjects lineage.events.>) that the Dapr pubsub.jetstream
component publishes to / consumes from. Dapr's jetstream component does NOT auto-create the stream,
so this Job does it (idempotent: skip if it already exists), on the bus `nats.url` names. The bus is
a prerequisite (ADR 0153), and the streams are rask's: without them every component would point at a
server with no streams and every publish would fail.

NOT a hook — see "BOOTSTRAP JOBS" in _helpers.tpl. A daprd sidecar subscribes at startup, so an
absent stream is a startup-time dependency of half the fleet; running the provisioner only after
`--wait` had already declared those pods hopeless is the wrong order in the same way the OpenFGA
migration was.

## `ttlSecondsAfterFinished: 3600`

Reap the finished pod. Each revision's job carries a -r<revision> name, so Helm's
before-hook-creation delete policy never matches the previous one — without a TTL every
`helm upgrade` left another Completed husk behind (14 were sitting in `kubectl get pods`).

## `labels:`

The explicit component label (the P4 landmine, see bootstrap-admin.yaml): nats:4222 is pod-open
under the prod NetworkPolicy so nothing blocks today, but the label keeps this pod visible to
release inventory (`kubectl get pods -l app.kubernetes.io/instance=…`) and admissible the day the
nats ingress gets an exclusive client list like openbao/age/minio.

## `env:`

the nats CLI writes its context under $HOME, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses.

## `- { name: NATS_CREDS, value: /etc/nats-creds/nats.creds }`

The path of the `admin` creds file the nats CLI reads (values.yaml `nats.auth`), never the credential itself.

## `until err=$(nats --server "$S" stream ls 2>&1 >/dev/null); do`

A server that is not up yet is waited for; one that refuses this Job's credential fails it now, by name.

## `converge_max_ack_pending() {`

A CONSUMER'S CONFIG DOES NOT SELF-HEAL FROM THE COMPONENT, and that is why this is
here rather than trusted to Dapr. MEASURED 2026-09-22: adding `maxAckPending` to the
work component left the LIVE durable — created 2026-09-03 — on NATS's default of
1,000, while the chart, the rendered Component and the gate all said 80. Deleting the
durable and letting Dapr rebuild it produced `Max Ack Pending: 80`, so the key IS
honoured, but only at CREATION. An estate that already has the consumer keeps the old
bound silently and forever.

Unlike a STREAM's retention this CAN be converged in place — `nats consumer edit
--max-pending=N --force` is non-interactive (verified against the live broker before
this was written, the lesson [[LH-151]]'s silent `stream add` failure taught) — so
this repairs rather than refusing and telling an operator to drain something.
