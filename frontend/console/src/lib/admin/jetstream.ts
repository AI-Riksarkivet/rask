import * as v from 'valibot';

// The JetStream overview the /admin/streams panel renders: streams with their config and state, and each
// consumer's lag (pending / ack-pending / redelivered). No route serves the NATS monitor to the console,
// so `jetstream.api.ts` answers 501 and the panel renders that; the shape stays the panel's contract.

// --- trimmed overview (the browser-facing shape) -----------------------------------------------------

export const JetStreamConsumerSchema = v.object({
	name: v.string(),
	// The estate SERVICE behind this consumer: the deliver group when present (Dapr's
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
