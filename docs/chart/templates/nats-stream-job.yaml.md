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
nats ingress gets an exclusive client list like openbao/age/the object store.

## `env:`

the nats CLI writes its context under $HOME, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses.

## `- { name: NATS_CREDS, value: /etc/nats-creds/nats.creds }`

The path of the `admin` creds file the nats CLI reads (values.yaml `nats.auth`), never the credential itself.

## `until err=$(nats --server "$S" stream ls 2>&1 >/dev/null); do`

A server that is not up yet is waited for; one that refuses this Job's credential fails it now, by name.

## `consumer_of() {`

The drift passes judge a durable by its info, and a judgement on a failed read deleted a healthy durable, which the
sidecar re-creates at `deliverPolicy: new`, skipping what was published in between ([[LH-343]]). Measured 2026-10-27 on
the nats-auth rig with an admin that may list and delete but not read: every durable on LINEAGE, DLQ and CATALOG_CONTROL was
deleted and the Job exited 0. The CLI's `consumer info` cannot tell a missing consumer from a failed read (on a missing
name nats-box 0.14.5's CLI v0.1.5 tries to prompt for one, "cannot pick a Consumer without a terminal"), so the reader
asks `$JS.API.CONSUMER.INFO` itself: error 10014 is a consumer that does not exist (nothing to judge), a `config` is
one read, and anything else (a timeout during a leader move, a refusal, no server) is retried five times, 3 s apart,
then fails the Job with that durable left in place; `backoffLimit` runs it again.

## `LIN="{{ .Values.services.lineage.daprAppId }}-durable"`

LINEAGE'S DURABLE IS HELD TO ITS IN-FLIGHT BOUND ([[LH-533]]). `services.lineage.deliveriesInFlight` is the
durable's `max_ack_pending`, the one config field the chart sets beyond `max_deliver` and `backoff`, and a
sidecar cannot rebind a durable whose config differs ("consumer name already in use"), so a durable at another
value is deleted for the sidecar to recreate. This Job runs beside the rollout, so the outgoing pod's sidecar
may recreate the durable as it was: the Job checks again every 5 s and deletes it again until it is at the
bound, and after 300 s, which outlasts the outgoing pod's grace period, it fails for its retry if a durable is
still at another value. Each recreation starts at the stream's first message (`deliverPolicy: all`), a replay
the keyed ingest lands nothing twice from.

## `BROADCAST_INACTIVE_NS="{{ mul (int (required "catalog.controlConsumerInactiveSeconds is required" .Values.catalog.controlConsumerInactiveSeconds)) 1000000000 }}"`

The mirror CATALOG_CONTROL_BROADCAST holds each catalog replica's durable ([[LH-305]]); its consumer limit is what
reaps a deleted pod's, so the Job declares it in git and converges it: a mirror whose limit or message age differs from
the value is edited from the same config (`nats stream edit --config`, measured on nats-server v2.14.2 to update both).
The server refuses a change to a stream's consumer limits while any consumer exceeds them, comparing `max_ack_pending`
too (v2.14.2 `stream.go:2420-2446`), so the limits name `max_ack_pending: 1000`, the server's default for a consumer
that sets none, and a lowered limit first lowers each durable above it (`nats consumer edit --inactive-threshold`):
measured, a raise applies directly, and a lower is refused until the durables are lowered.
A consumer takes the stream's limit when it is created or re-bound (measured: a second create of one durable with a new
deliver subject is accepted and carries the limit), so an edited limit reaches each replica's durable when its sidecar
next subscribes. The message age equals the limit, so a replica back within it resumes at its acknowledgement floor. The
config is written whole because `nats stream add --mirror` asks for the mirror's details even under `--defaults`
(nats-box 0.14.5's CLI v0.1.5), which a Job without a terminal cannot answer. Before the Job creates the mirror on an
upgrade, a rolled catalog pod's subscribe fails on the missing stream and daprd retries it (dapr
`pkg/runtime/processor/subscriber/subscriber.go:354-360`).
