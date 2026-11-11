# chart/templates/nats-stream-job.yaml

Rationale for `chart/templates/nats-stream-job.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `apiVersion: batch/v1`

Declare the JetStream streams (LINEAGE, subjects lineage.events.>, and DLQ and the catalog's control streams) that the
Dapr pubsub.jetstream components publish to and consume from. Dapr's jetstream component does NOT auto-create a stream,
so this Job does it on the bus `nats.url` names, and on every run converges each stream to the config the chart
declares (`converge_stream` below). The bus is a prerequisite (ADR 0153), and the streams are rask's: without them every
component would point at a server with no streams and every publish would fail.

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

## `converge_stream() {`

EVERY STREAM CONVERGES TO THE CONFIG THE CHART DECLARES ([[XC-164]]). A stream created once keeps its creation config
while the chart moves on, so a changed max age (`nats.streamMaxAgeSeconds`), replica count (`nats.streamReplicas`) or
retention would reach no running bus, and a reconciler re-applying git would change nothing. Each run therefore
writes the stream's whole config (`declare_stream`), creates the stream from it when absent and edits the stream to it in
place when present (`nats stream edit --config -f`). JetStream refuses some edits in place: storage, a mirror, retention
to or from workqueue, lifting `deny_delete` or `deny_purge` (nats-server v2.14.2 `jsa.configUpdateCheck`); the Job then
fails with the server's reason and names the hand step (drain, `nats stream rm`, re-run), rather than leaving a stream
the chart does not describe. Measured on natscli v0.1.5 against nats-server 2.14.2: an edit to an unchanged config exits
0, an allowed change (max age, retention `interest` to `limits`) applies and exits 0, a refused one exits 1 naming the
reason (`... can not change retention policy to/from workqueue (10052)`); natscli 0.5.1 (nats-box 0.20.2) against
nats-server 2.15.2 answered an unchanged config and a refused one the same way (2026-11-09). The Job's bus user (`nats.auth.users.admin`)
holds `$JS.API.STREAM.UPDATE` for exactly the streams it edits, so an operator-mode bus admits the edits and nothing
wider; the `nats-auth` lane runs this Job as that user against such a bus. `allow_direct: true` is in the declared config
because `nats stream add --defaults` sets it, so a stream created that way edits to no change, and a config without it
would turn it off. The mirror's `mirror_direct` is the server's: it derives `true` for a mirror of a stream that allows direct
gets, on create and on an edit that omits it (measured on nats-server 2.14.2), so the declared config leaves it out. `tests/unit/test_the_stream_job_converges_each_stream_to_its_declared_config.py` runs the script against a
stand-in CLI; the GitOps lane reads a running bus back after a change.

## `converge_bucket() {`

A key-value bucket's stream converges to its declared config as every stream does, but not through `nats stream edit`.
natscli recognises a bucket's stream and asks its own question before editing it ("Interacting with KV buckets outside of
the 'nats kv' subcommand can lead unexpected outcomes ... Continuing this operation is an unsupported action"), which
`-f` does not answer, so without a terminal the edit fails (`nats: error: cannot ask for confirmation without a
terminal`) on every run after the one that created the bucket, and the next upgrade's Job with it. `nats kv edit` asks
nothing but cannot keep rollups off, and `nats kv add` makes buckets that allow them. So the Job sends JetStream's own
update request, `$JS.API.STREAM.UPDATE.<stream>` carrying the whole declared config: the request `nats stream edit`
sends, under the same grant. It exits 0 however it is answered, so the Job reads the answer: the stream's info
(`"config":`) for a config applied or already in place, an error naming the reason for one JetStream refuses (storage,
lifting `deny_delete`: 10052), which fails the Job with the hand step a refused stream edit names, and neither when the
request was refused by the bus's permissions or answered by no one, which fails the Job asking for a re-run, since
draining the bucket would not help. Measured on
natscli 0.5.1 (nats-box 0.20.2) against nats-server 2.15.2 (2026-11-09): an unchanged config and a doubled max age and
duplicate window were applied, a storage change and a lifted `deny_delete` were refused with 10052, rollups turned on by
hand were turned back off, and a request no one answered exited 0. A bucket that does not exist yet is created with
`nats stream add`, which asks nothing. The `nats-auth` lane re-runs this Job as `admin` on a running three-node bus after
changing `maintenance.queue.ackWaitSeconds`, and reads the bucket's max age back.

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
reaps a deleted pod's, so the Job declares it in git and converges it: the mirror is edited to the same config on every run
(`nats stream edit --config`, measured on nats-server v2.14.2 to update both its limit and its message age).
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

## `converge_stream MAINTENANCE_TASKS`

MAINTENANCE_TASKS is maintenance's work queue (ADR 0198 §2). It keeps one task per subject and refuses a second
(`max_msgs_per_subject` 1, `discard_new_per_subject`), so a table is never queued or passed twice at once. The
schedules (NATS message schedules, ADR-51) sit apart on MAINTENANCE_SCHEDULES: each holds when its task is next due, a
newer one replacing it, and a schedules stream may not discard new. Both are work queues: an ack deletes, and no
consumer reads a held schedule. A schedules stream is served with `allow_rollup_hdrs` on and `deny_purge` off whatever
its config asks (asking otherwise is overridden, and `deny_purge` beside rollups is refused, 10052), so it is declared
that way, and the worker asserts the audit's and the scan's schedules at its start and every 15 minutes
(`maintenance.services.tasks`), which brings them back after a purge. Measured on nats-server 2.15.2 with file storage
(2026-11-09).

The Job then reads MAINTENANCE_SCHEDULES back with a raw `$JS.API.STREAM.INFO` request and fails unless its config says
`allow_msg_schedules: true`. A server older than 2.12 drops the field it does not know and reports the stream created:
measured on nats-server 2.11.10 (2026-11-10), the Job otherwise exited 0 with a schedules stream on which no schedule
would ever fire. The raw request is read rather than `nats stream info --json`, which re-serialises the config through the
CLI's own types.

## `printf '%s' '{"name":"KV_MAINTENANCE_CLAIMS",...}' > "$HOME/KV_MAINTENANCE_CLAIMS.json"`

The MAINTENANCE_CLAIMS key-value bucket holds the claim a pass takes on its table before it runs (ADR 0198): created only
where none stands, renewed with each heartbeat against its revision, released only by its holder (its revision, or the revision its own token
stands at when a renewal's answer was lost), and gone one
ack wait after its last renewal, since the bucket's `max_age` is `maintenance.queue.ackWaitSeconds`. So a worker's claim
outlives its lease, and a pass that finds another's claim waits it out: a settle the network held past a stale worker's
lease, which JetStream applies late and which frees the table's task under a live pass, can no longer let a second pass of
the table start beside it. A stale holder's late renewal or release cannot match the revision now standing (measured on
nats-server 2.15.2, three nodes, R3, 2026-11-09). The bucket is the stream nats-py's `create_key_value` would make, less
rollups, which would let one publish on the worker's grant wipe every claim; it is declared here so the worker binds to it
and never creates it, and `admin` holds UPDATE for it as for every stream this Job edits (`converge_bucket`), so a changed
`maintenance.queue.ackWaitSeconds` converges its max age with the consumer's ack wait.

## `{{- if gt (mul 2 (int $queue.heartbeatSeconds)) (int $queue.ackWaitSeconds) }}`

A running maintenance task stays with its worker only while the worker's in-progress acknowledgement reaches the broker
inside the `maintenance-worker` consumer's ack wait; once one is late, JetStream delivers the task to another replica
while the first is still passing the table. So the render refuses a heartbeat past half the ack wait, naming both values
(`tests/unit/test_invariants.py::test_a_queue_value_the_worker_cannot_run_with_fails_the_render`). The worker's side is
`tests/e2e-py/test_two_workers_share_the_queue_and_a_dead_ones_task_goes_to_the_other.py`: a live worker's task stayed
with it for three ack waits, a killed one's moved to the other, and one the bus stopped answering ended itself before its
task moved.

## `converge_consumer MAINTENANCE_TASKS maintenance-worker`

The consumer delivers a task once more than `maintenance.queue.maxDeliver`. A worker whose run fails on the last
delivery ends the task itself, but a pass that takes its replica down (out of memory, or past twice
`maintenance.queue.maxPassSeconds`, when the worker exits) never gets there, and JetStream keeps a work-queue message
whose deliveries ran out (measured on nats-server 2.15.2, 2026-11-09): it would hold its table's subject, refusing
every later pass of the table, until the stream's day-long `max_age`. The extra delivery reaches a worker, which ends
the task without running it, counts it (`maintenance.tasks.ended`, the alert MaintenanceTasksEnded) and holds its next
try `maintenance.queue.recheckHours` out, since the hourly audit queues a table nothing holds.

## `converge_stream STORE_EVENTS`

STORE_EVENTS holds the store's change events (ADR 0198 §3): each object write of a rask bucket, published on
`store.events` by the store and read by maintenance alone. Its duplicate window covers the store's retry lifetime,
since RustFS 1.0.3 refuses a stream whose window is shorter (274 s at its 30 s ack timeout; measured 2026-11-09). An
event is a hint: the table's daily recheck finds what a lost one would have reported. So the stream drops its oldest
events past `maintenance.queue.storeEventsMaxBytes` (`discard` old) rather than refusing the store, because it shares
the bus's JetStream disk with LINEAGE and the work queue, and a long maintenance outage must not fill that disk for
them. One event is about 2 KB (RustFS 1.0.3), so the default 128 MiB holds about 65,000.
