import { env } from '$env/dynamic/private';
import { json } from '@sveltejs/kit';
import { parse } from '@rask/api';
import { RawJszSchema, type JetStreamOverview } from '$lib/admin/jetstream';
import type { RequestHandler } from './$types';

// `/api/jetstream` — read-only JetStream visibility for the /streams admin panel. The NATS HTTP monitor
// port (8222) is unauthenticated by design and reachable only over its in-cluster ClusterIP, so — exactly
// like the GreptimeDB seam behind /api/audit — this BFF's admin gate is the ONLY gate: the monitor URL is
// never exposed to the browser or the ingress. We fetch `/jsz` server-side, parse it, and return a trimmed
// typed overview (the raw payload is ~29 KB; the browser gets only what the panel renders). No mutation
// surface exists on the monitor endpoints, matching the read-only viewer posture.
//
// Session-gated on a governed stack (mirroring /api/audit): stream/consumer topology describes the whole
// estate's event fabric, so an anonymous front-door caller must not read it; auth-off dev answers openly.
// 501 when NATS_MONITOR_API is unset (no NATS monitor wired).
const NATS_MONITOR_API = env.NATS_MONITOR_API ?? '';
// The view is ESTATE-wide (every stream/consumer in the fabric), so it gates on the ESTATE-admin
// authority — the catalog's `can_observe_events` on the fixed root object, the same rung `/v1/events`
// and `/v1/projects` check. A per-project door here would hand the configured project's admins the whole
// estate's topology while 403ing an estate owner not on that project — the exact scope bug
// docs/DECISIONS.md #control-events--estate-admin-scope records for /v1/events. (audit 2026-07-24)
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';
// The dead-subscription detector: JETSTREAM_EXPECTED_CONSUMERS (chart _helpers.tpl frontendEnv) is a
// comma list of "STREAM:service" rendered from the SAME values the Dapr pubsub components render, so the
// expectation cannot drift from what the estate actually subscribes. An expected group with no live
// consumer is INVISIBLE in raw /jsz — the app sits Ready while its trigger stream goes unread (the silent
// cascade stall, 2026-07-13) — so the BFF diffs expected vs present and returns the gap as `missing`.
const EXPECTED_CONSUMERS: readonly { stream: string; service: string }[] = (
	env.JETSTREAM_EXPECTED_CONSUMERS ?? ''
)
	.split(',')
	.map((entry) => entry.trim())
	.filter((entry) => entry.length > 0)
	.flatMap((entry) => {
		const sep = entry.indexOf(':');
		// A malformed segment (no separator, empty stream/service) is dropped rather than fabricating a
		// forever-missing phantom — the helm helper is the single source and renders well-formed pairs.
		if (sep <= 0 || sep === entry.length - 1) return [];
		return [{ stream: entry.slice(0, sep), service: entry.slice(sep + 1) }];
	});

// Per-consumer service label: Dapr's queueGroupName IS the subscriber app-id, so the deliver group names
// the service directly. The catalog control broadcast is deliberately group-less (every replica gets every
// event), so a no-group ephemeral on CATALOG_CONTROL is a catalog replica; any other group-less ephemeral
// (e.g. a nats-cli inspection consumer) gets the honest "(ephemeral)" placeholder.
function serviceLabel(bareStream: string, deliverGroup: string | undefined): string {
	if (deliverGroup) return deliverGroup;
	if (bareStream === 'CATALOG_CONTROL') return 'catalog (broadcast replica)';
	return '(ephemeral)';
}

// Returns null when the caller is an estate admin; otherwise the {status, detail} to answer with. The
// probe is a side-effect-free `GET /v1/events` with a past-any-head cursor: an estate admin gets an
// empty page (an empty poll is never audited, so the probe leaves no trail noise), anyone else the
// catalog's own 401/403 verdict. We bearer-FORWARD the user's token (never a service token) and only
// query the NATS monitor if it returns 200. Governed stack but no reachable catalog → fail closed.
async function estateAdminGate(
	fetchFn: typeof fetch,
	accessToken: string,
): Promise<{ status: number; detail: string } | null> {
	try {
		const res = await fetchFn(`${CATALOG_API}/v1/events?since=${Number.MAX_SAFE_INTEGER}`, {
			headers: { authorization: `Bearer ${accessToken}` },
			signal: AbortSignal.timeout(5000),
		});
		if (res.ok) return null;
		if (res.status === 403) return { status: 403, detail: 'the stream view is estate-admin only' };
		if (res.status === 401) return { status: 401, detail: 'sign in to view JetStream streams' };
		return { status: 503, detail: 'jetstream admin authorization is unavailable' };
	} catch (err) {
		console.error(`api jetstream admin-gate upstream failure: ${String(err)}`);
		return { status: 503, detail: 'jetstream admin authorization is unavailable' };
	}
}

export const GET: RequestHandler = async ({ fetch, locals }) => {
	if (locals.authEnabled && !locals.session) {
		return json({ detail: 'sign in to view JetStream streams' }, { status: 401 });
	}
	// Authorization (not just authentication): a governed, signed-in caller must be an ESTATE admin — else
	// any project's admin could map the estate's whole event fabric. Auth-off dev skips the gate (open).
	if (locals.authEnabled && locals.session) {
		const denied = await estateAdminGate(fetch, locals.session.accessToken);
		if (denied) return json({ detail: denied.detail }, { status: denied.status });
	}
	if (!NATS_MONITOR_API) {
		return json({ detail: 'jetstream monitor not configured' }, { status: 501 });
	}
	try {
		// Bounded: a wedged monitor (TCP accepted, no headers) must yield a fast honest 502, not pin the
		// request on undici's ~300s default.
		const res = await fetch(`${NATS_MONITOR_API}/jsz?streams=true&consumers=true&config=true`, {
			signal: AbortSignal.timeout(5000),
		});
		if (!res.ok) {
			return json({ detail: `nats monitor ${res.status}` }, { status: 502 });
		}
		const raw = parse(RawJszSchema, await res.json());
		// Trim: flatten account_details[].stream_detail[] and keep only what the panel renders — the browser
		// never sees the raw monitor payload. Stream names are unique only PER ACCOUNT, so with multiple
		// accounts the name is account-qualified (the panel keys its each-block on it).
		const multiAccount = raw.account_details.length > 1;
		// Consumer groups per BARE stream name (expected/present matching must ignore the account
		// qualifier below), split by BOUNDNESS: a durable consumer outlives its subscriber, so mere
		// existence must not count as present (the dead-subscription false negative, audit 2026-07-23).
		// A consumer is bound when NATS says a push subscription is attached (`push_bound`) or a pull
		// client is actively waiting (`num_waiting` > 0). "*" marks a group-less ephemeral on
		// CATALOG_CONTROL — the catalog broadcast consumer carries no deliver group by design, so ANY
		// such (bound) consumer satisfies the expected catalog entry.
		const bound = new Map<string, Set<string>>();
		const existing = new Map<string, Set<string>>();
		const mark = (into: Map<string, Set<string>>, stream: string, group: string) => {
			const groups = into.get(stream) ?? new Set<string>();
			groups.add(group);
			into.set(stream, groups);
		};
		const streams = raw.account_details
			.flatMap((account) => account.stream_detail.map((s) => ({ account: account.name, s })))
			.map(({ account, s }) => ({
				name: multiAccount ? `${account}/${s.name}` : s.name,
				subjects: s.config.subjects,
				retention: s.config.retention,
				storage: s.config.storage,
				max_age_ns: s.config.max_age,
				max_msgs: s.config.max_msgs,
				max_bytes: s.config.max_bytes,
				num_replicas: s.config.num_replicas,
				state: s.state,
				consumers: s.consumer_detail.map((c) => {
					const group = c.config?.deliver_group;
					const isBound = c.push_bound === true || c.num_waiting > 0;
					const key = group ?? (s.name === 'CATALOG_CONTROL' ? '*' : null);
					if (key !== null) mark(isBound ? bound : existing, s.name, key);
					return {
						name: c.name,
						service: serviceLabel(s.name, group),
						durable: Boolean(c.config?.durable_name),
						deliver_group: group,
						num_pending: c.num_pending,
						num_ack_pending: c.num_ack_pending,
						num_redelivered: c.num_redelivered,
						last_active: c.delivered?.last_active,
					};
				}),
			}))
			.sort((a, b) => a.name.localeCompare(b.name));
		// Expected-vs-bound diff: an expected entry whose stream has no BOUND matching group (a missing
		// stream counts every expectation on it as missing) is a dead subscription. Order follows the env
		// list — the same order the Dapr components render in. When a consumer for the group exists but
		// nothing is attached (an orphaned durable), the entry is flagged `unbound` so the panel can name
		// the more deceptive flavor honestly ("present but unbound").
		const isSatisfied = (m: Map<string, Set<string>>, stream: string, service: string) => {
			const groups = m.get(stream);
			return groups?.has(service) === true || groups?.has('*') === true;
		};
		const missing = EXPECTED_CONSUMERS.filter(
			({ stream, service }) => !isSatisfied(bound, stream, service),
		).map(({ stream, service }) => ({
			stream,
			service,
			unbound: isSatisfied(existing, stream, service),
		}));
		const overview: JetStreamOverview = {
			now: raw.now,
			totals: {
				streams: raw.streams,
				consumers: raw.consumers,
				messages: raw.messages,
				bytes: raw.bytes,
			},
			streams,
			missing,
		};
		return json(overview);
	} catch (err) {
		// Fixed detail: a ValiError's message can echo received-value fragments of the raw monitor payload —
		// keep the specifics in the server log, never in the response body.
		console.error(`api jetstream proxy upstream failure: ${String(err)}`);
		return json(
			{ detail: 'jetstream monitor unreachable or its payload did not parse' },
			{ status: 502 },
		);
	}
};
