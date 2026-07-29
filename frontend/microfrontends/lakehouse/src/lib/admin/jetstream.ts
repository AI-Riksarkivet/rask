import * as v from 'valibot';

// JetStream visibility wire contracts (read-only, #20-adjacent ops surface). Two boundaries, both parsed
// (never cast) per the @rask/api parse-don't-validate rule:
//  1. the RAW NATS `/jsz?streams=true&consumers=true&config=true` monitor payload, fetched SERVER-SIDE by
//     the /api/jetstream BFF (the monitor port is unauthenticated by design — the BFF adminGate is the only
//     gate, so the raw shape must never reach the browser);
//  2. the TRIMMED overview the BFF returns — the ~29 KB raw payload reduced to what the panel renders.
// Schemas are derived from a real live /jsz sample (kind cluster, 2026-07-23): durations are int64
// NANOSECONDS; streams live under account_details[].stream_detail[]; consumers carry
// num_pending/num_ack_pending/num_redelivered; push consumers have deliver_group; ephemeral consumers lack
// durable_name. `v.object` strips unknown keys, so the raw schema names only what the trim needs.

// --- raw /jsz subset ---------------------------------------------------------------------------------

const RawStreamConfigSchema = v.object({
	// `subjects` is optional defensively: sourced/mirrored streams may omit it.
	subjects: v.optional(v.array(v.string()), []),
	retention: v.string(), // "limits" | "interest" | "workqueue"
	storage: v.string(), // "file" | "memory"
	max_age: v.number(), // int64 ns; 0 = unlimited
	max_msgs: v.number(), // -1 = unlimited
	max_bytes: v.number(), // -1 = unlimited
	num_replicas: v.number(),
});

const RawConsumerSchema = v.object({
	name: v.string(),
	config: v.optional(
		v.object({
			durable_name: v.optional(v.string()), // absent on ephemeral consumers
			deliver_group: v.optional(v.string()), // push consumers only
		}),
	),
	delivered: v.optional(v.object({ last_active: v.optional(v.string()) })),
	num_ack_pending: v.number(),
	num_redelivered: v.number(),
	num_pending: v.number(),
	// Boundness: a durable consumer OUTLIVES its subscriber, so mere existence in /jsz does not prove
	// anything reads it (the dead-subscription false negative, audit 2026-07-23). For push consumers
	// NATS reports `push_bound` (true only while a subscription is attached); pull consumers have no
	// such flag — `num_waiting` (outstanding pull requests) > 0 is the only positive liveness signal.
	// Both optional defensively: each appears only for its consumer flavor.
	push_bound: v.optional(v.boolean()),
	num_waiting: v.optional(v.number(), 0),
});

const RawStreamSchema = v.object({
	name: v.string(),
	config: RawStreamConfigSchema,
	state: v.object({
		messages: v.number(),
		bytes: v.number(),
		first_seq: v.number(),
		last_seq: v.number(),
		consumer_count: v.number(),
	}),
	consumer_detail: v.optional(v.array(RawConsumerSchema), []),
});

/** The `/jsz` monitor response, reduced to the fields the trim consumes. */
export const RawJszSchema = v.object({
	now: v.string(), // ISO-8601 UTC
	streams: v.number(),
	consumers: v.number(),
	messages: v.number(),
	bytes: v.number(),
	// Absent entirely when JetStream holds no streams yet.
	account_details: v.optional(
		v.array(
			v.object({ name: v.string(), stream_detail: v.optional(v.array(RawStreamSchema), []) }),
		),
		[],
	),
});

// --- trimmed overview (the browser-facing shape) -----------------------------------------------------

export const JetStreamConsumerSchema = v.object({
	name: v.string(),
	// The estate SERVICE behind this consumer, derived server-side: the deliver group when present (Dapr's
	// queueGroupName IS the app-id), else the catalog broadcast replica on CATALOG_CONTROL, else the
	// "(ephemeral)" placeholder for a group-less ephemeral (e.g. a nats-cli inspection consumer).
	service: v.string(),
	durable: v.boolean(),
	deliver_group: v.optional(v.string()),
	num_pending: v.number(),
	num_ack_pending: v.number(),
	num_redelivered: v.number(),
	last_active: v.optional(v.string()),
});
export type JetStreamConsumer = v.InferOutput<typeof JetStreamConsumerSchema>;

const JetStreamStreamSchema = v.object({
	name: v.string(),
	subjects: v.array(v.string()),
	retention: v.string(),
	storage: v.string(),
	max_age_ns: v.number(),
	max_msgs: v.number(),
	max_bytes: v.number(),
	num_replicas: v.number(),
	state: v.object({
		messages: v.number(),
		bytes: v.number(),
		first_seq: v.number(),
		last_seq: v.number(),
		consumer_count: v.number(),
	}),
	consumers: v.array(JetStreamConsumerSchema),
});

/** An expected consumer group (from JETSTREAM_EXPECTED_CONSUMERS) with no BOUND consumer on its stream —
 * a dead subscription: the app looks Ready while nothing reads its trigger stream (the silent cascade
 * stall of 2026-07-13). Absence is invisible in the raw monitor payload; this diff is what surfaces it.
 * `unbound` distinguishes the two flavors: false = no consumer exists at all; true = a consumer (e.g. a
 * durable that outlived its subscriber) EXISTS but nothing is attached to it — equally dead, and even
 * more deceptive because the raw topology looks populated. */
const JetStreamMissingConsumerSchema = v.object({
	stream: v.string(),
	service: v.string(),
	unbound: v.optional(v.boolean(), false),
});

export const JetStreamOverviewSchema = v.object({
	now: v.string(),
	totals: v.object({
		streams: v.number(),
		consumers: v.number(),
		messages: v.number(),
		bytes: v.number(),
	}),
	streams: v.array(JetStreamStreamSchema),
	missing: v.array(JetStreamMissingConsumerSchema),
});
export type JetStreamOverview = v.InferOutput<typeof JetStreamOverviewSchema>;
