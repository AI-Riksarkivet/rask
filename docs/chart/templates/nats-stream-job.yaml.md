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
