# chart/templates/nats-stream-job.yaml

Rationale for `chart/templates/nats-stream-job.yaml`. It lives here because Helm stores the chart's templates in
every release revision, under a 1 MiB ceiling. Each section is headed by the template line it explains.

## `{{- range $phase := list "declare" "retire" }}`

The template renders two Jobs, one per phase, from one script body.

The DECLARE phase, `<release>-nats-stream-r<rev>`, declares the JetStream streams (LINEAGE, subjects
lineage.events.>, DLQ, CATALOG_CONTROL under `catalog.controlEmit`, and maintenance's streams under
`maintenance.enabled`) and the durable consumers the services pull (`lance.busConsumers` in `_helpers.tpl`), on the bus
`nats.url` names. No service creates a stream or a consumer: each binds to what this Job declared, and its bus user
holds no grant to create one. So on every run the Job converges each stream and each consumer to the config the chart
declares (`converge_stream`, `converge_consumer` below). The bus is a prerequisite (ADR 0153), and the streams and
consumers are rask's: without them every publish would fail and every pull would bind to nothing.

The declare phase is NOT a Helm hook (see "BOOTSTRAP JOBS" in _helpers.tpl): it is a plain manifest Job, applied in the
same wave as the Deployments, and under Argo CD a `Sync` hook (`lance.reconciledJob`). A service whose consumer is not
declared yet asks for it again (`bus_pull_failed`, said once per fault) until the Job has run, so nothing waits on an
order the chart would have to know.

The RETIRE phase, `<release>-nats-stream-retire-r<rev>`, removes what git no longer declares. It is a Helm
`post-install,post-upgrade` hook and, under Argo CD, a `PostSync` hook: it runs after the sync, once no pod of the
previous revision is left to pull a consumer it removes.

## `ttlSecondsAfterFinished: 3600`

Reap the finished pod. Each revision's job carries a -r<revision> name, so Helm's
before-hook-creation delete policy never matches the previous one — without a TTL every
`helm upgrade` left another Completed husk behind (14 were sitting in `kubectl get pods`).

## `labels:`

The explicit component label (the P4 landmine): `nats-stream` is on the bus's exclusive
client list (`lance.busClients`), which admits it to the development bus's 4222 (network-policy.yaml (3f)) and to the
platform bus's egress on a real deployment ([[XC-202]]); a Job without it reaches no bus under the isolation layer.
Both phases carry it.

## `seccompProfile: { type: RuntimeDefault }`

The pod states only its seccomp profile ([[XC-202]]): its one container runs the nats-box image as root
(`lance.rootSecurityContext`, with every capability dropped and a read-only root), so `runAsNonRoot`, which
`lance.podSecurityContext` sets, would refuse the pod.

## `env:`

the nats CLI writes its context under $HOME, which a read-only rootfs turns into a failure. HOME on the tmp
emptyDir is the pair every app container in this chart already uses.

## `- { name: NATS_CREDS, value: /etc/nats-creds/nats.creds }`

The path of the `admin` creds file the nats CLI reads (values.yaml `nats.auth`), never the credential itself.

## `until err=$(nats --server "$S" stream ls 2>&1 >/dev/null); do`

A server that is not up yet is waited for; one that refuses this Job's credential fails it now, by name.

## `consumer_of() {`

Both phases judge a consumer by its info: the declare phase reads the start a consumer holds, and the retire phase reads
its ownership mark before it removes it. A judgement on a failed read must not act ([[LH-343]]: measured 2026-10-27 on
the nats-auth rig with an admin that may list and delete but not read, a pass that judged on a failed read deleted every
durable on LINEAGE, DLQ and CATALOG_CONTROL and the Job exited 0). The CLI's `consumer info` cannot tell a missing
consumer from a failed read (on a missing name nats-box 0.14.5's CLI v0.1.5 tries to prompt for one, "cannot pick a
Consumer without a terminal"), so the reader asks `$JS.API.CONSUMER.INFO` itself: error 10014 is a consumer that does
not exist (an empty answer), a `config` is one read, and anything else (a timeout during a leader move, a refusal, no
server) is retried five times, 3 s apart, then fails the Job with that consumer left in place; `backoffLimit` runs it
again.

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
would turn it off. `tests/unit/test_the_stream_job_converges_each_stream_to_its_declared_config.py` runs the script against a
stand-in CLI; the GitOps lane reads a running bus back after a change.

RETENTION IS A FAN-OUT CORRECTNESS INVARIANT on a stream several consumers read (LINEAGE is read by `lineage-ingest`
and `maintenance-arrival`, each of which must see every event): `limits`, never `workqueue`, whose first ack deletes a
message so the consumers take deliveries from each other, and never `interest`, which deletes a message once every
consumer has acked it, so the history a lineage rebuild replays is gone. The edit restores `limits` from `interest` in
place; from `workqueue` it is refused and the Job fails naming why.

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

## `converge_consumer() {`

A DURABLE CONSUMER CONVERGES TO ITS DECLARED CONFIG AS A STREAM DOES: created when absent, edited in place when present
(`nats consumer edit --config -f`), so a changed ack wait, in-flight bound, filter or mark reaches a consumer that
already exists, and a service never has to delete and re-create one to change it. What JetStream refuses to change in
place (where a consumer starts, its `max_waiting`) fails the Job with the server's own reason. Measured on natscli 0.5.1
(nats-box 0.20.2) against nats-server 2.15.2, 2026-11-14: an edit to an unchanged config exits 0; a changed `ack_wait`,
`max_ack_pending`, filter or metadata is applied in place; a changed start sequence or `max_waiting` is refused
(`... can not be updated (10012)`). The server adds `_nats.*` keys of its own to a consumer's metadata, and an edit that
omits them does not fail.

The rendered declare and retire scripts were run against a scratch nats-server 2.15.2 (no auth), 2026-11-14: a fresh bus
is declared, and a second run converges all 12 objects; changed values edit `lineage-ingest` in place and keep its
start.

## `declare_stream DLQ "dlq.>"`

DLQ holds what a subscriber outside this release parks on `dlq.<its own name>`. Nothing of the lakehouse's is published
there: lineage keeps a delivery it ends in the provenance namespace's `refusals` table, and the other lanes end theirs
on the bus and count them. The stream stays declared so such a subscriber has a place to park without a stream of its
own.

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

## `converge_stream STORE_EVENTS`

STORE_EVENTS holds the store's change events (ADR 0198 §3): each object write of a rask bucket, published on
`store.events` by the store and read by maintenance alone. Its duplicate window covers the store's retry lifetime,
since RustFS 1.0.3 refuses a stream whose window is shorter (274 s at its 30 s ack timeout; measured 2026-11-09). An
event is a hint: the table's daily recheck finds what a lost one would have reported. So the stream drops its oldest
events past `maintenance.queue.storeEventsMaxBytes` (`discard` old) rather than refusing the store, because it shares
the bus's JetStream disk with LINEAGE and the work queue, and a long maintenance outage must not fill that disk for
them. One event is about 2 KB (RustFS 1.0.3), so the default 128 MiB holds about 65,000.

## `declare_stream CATALOG_CONTROL "catalog.control.>"`

CATALOG_CONTROL carries the catalog's control-plane change events, under `catalog.controlEmit`. Every catalog replica
must see every event, so no consumer is declared on it: each replica follows the topic by reading the stream by
sequence on its own connection (`lakehouse.bus.Bus.follow`, nats-py's `get_msg`). A replica holds nothing on the bus,
so one that is gone leaves nothing to reap.

## `{{- range $consumers }}`

Every durable consumer the services pull comes from one derivation, `lance.busConsumers` in `_helpers.tpl`, which
both phases read: the declare phase converges each one, and the retire phase keeps each one. So a consumer the chart
declares is never the retire phase's to remove, and a consumer the chart stops declaring is removed by the next sync.
Each config carries the release's mark, `metadata: {"rask.dev/release": "<release name>"}`.

## `held=$(consumer_of {{ .stream }} {{ .name }}) || exit 1`

A CONSUMER'S START IS DECLARED BACK TO IT. `lineage-ingest` starts by stream sequence (`start: position` in
`lance.busConsumers`), and JetStream refuses to change a consumer's start in place (`... can not be updated (10012)`,
measured 2026-11-14 as above), so a config naming any other start than the one the consumer holds fails the edit. So
the Job reads the `opt_start_seq` the consumer
holds and writes it into the config it converges: the edit keeps the start and applies everything else.

## `held=$(consumer_of {{ .stream }} lineage-durable) || exit 1`

ONE-DEPLOY MIGRATION ([[XC-199]]), labelled in the script and removed with the next change to this Job. When
`lineage-ingest` does not exist yet, it begins after the ack floor of `lineage-durable`, the consumer it replaces, so
lineage's first pull consumer takes up exactly where the old subscription's acknowledgements end; with no such consumer
it begins at sequence 1. Measured on a scratch nats-server 2.15.2 prepared as the previous revision left the bus,
2026-11-14: `lineage-ingest` began at the old consumer's ack floor + 1.

The previous revision's consumer `LINEAGE/lineage-durable` acknowledged an event as it dead-lettered it, and only the
consumer that took the dead letter (`DLQ/lineage-dlq-durable`) kept it for the estate admin. A dead letter still waiting on that consumer at the
cut-over is therefore an event below the ack floor that nothing recorded or kept, and nothing of this revision reads
`dlq.lineage.events`. So the step reads that consumer too: while it holds a delivery pending or unacknowledged,
`lineage-ingest` begins at the stream's first message instead, and the lane is handed every retained event again
(each already recorded lands nothing new; the one that was parked is recorded, or parked into the refusals table).
Measured with the rendered scripts on a scratch nats-server 2.15.2, 2026-11-14: with one dead letter pending the
consumer began at sequence 1, with none at the ack floor + 1, and a declare after the retire kept either start.

## `MARK='"rask.dev/release":"{{ $.Release.Name }}"'`

WHAT GIT NO LONGER DECLARES IS REMOVED IN THE RETIRE PHASE. The phase lists every stream and every consumer on the bus,
and removes a consumer only when the chart does not declare it AND it carries this release's mark, which the declare
phase writes on each consumer it converges. A consumer with no mark, or another release's mark, is another
subscriber's and is never touched, and no stream is ever removed: a subscriber outside the release may hold a consumer
on any stream, and a stream holds data. Measured on a scratch nats-server 2.15.2, 2026-11-14: with
`maintenance.arrivalEnabled=false` the retire phase removes `maintenance-arrival`, and it leaves a consumer of another
subscriber. `nats consumer rm` and `nats stream rm` of an absent object exit 1 (natscli 0.5.1, 2026-11-14), so each
removal is of an object the phase has just read.

The phase runs after the sync (a Helm `post-install,post-upgrade` hook, an Argo CD `PostSync` hook), so nothing of the
new revision still pulls what it removes. Neither Helm's wait nor Argo CD's health waits for the previous revision's
terminating pods, so one of those may still be stopping: its pulls of a consumer that is gone are refused, which its
lane waits out until the pod ends (`lakehouse.bus` says a refused pull once and asks again).

## `for old in LINEAGE/lineage-durable LINEAGE/maintenance-durable DLQ/lineage-dlq-durable; do`

ONE-DEPLOY MIGRATION ([[XC-199]]), labelled in the script and removed with the next change to this Job. The consumers
the services' subscriptions were held on carry no mark, so the mark rule leaves them, and the mirror stream
`CATALOG_CONTROL_BROADCAST` is a stream, which the rule never removes. This step removes the three named consumers and
that mirror by name, each only when it exists (`admin` holds `$JS.API.STREAM.DELETE.CATALOG_CONTROL_BROADCAST` for
it, on a line of its own in `nats.auth.users` labelled for removal with this step). Measured on a scratch nats-server 2.15.2 prepared as the previous revision left the bus, 2026-11-14: the phase
removed the three consumers and the mirror and left a consumer of another subscriber.
