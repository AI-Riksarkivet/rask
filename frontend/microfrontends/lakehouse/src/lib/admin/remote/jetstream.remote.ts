import { getRequestEvent, query } from '$app/server';
import { env } from '$env/dynamic/private';
import { parse } from '@rask/api';
import type { ApiResult } from '@rask/api/client';
import { RawJszSchema, type JetStreamOverview } from '../jetstream';

// `/streams`' data plane, in the zone's remote-function dialect — the /api/jetstream route moved here
// VERBATIM (the transport ruling blocked-port #3, unblocked by the mock-observability harness): the NATS
// HTTP monitor is unauthenticated by design and in-cluster only, so this server-side seam stays the
// ONLY gate; the raw ~29 KB /jsz payload is trimmed to what the panel renders; and the estate-admin
// door (the catalog's `can_observe_events`, probed with a side-effect-free past-any-head /v1/events
// read) is enforced BEFORE the monitor is touched. Auth-off dev answers openly, exactly as before.

const NATS_MONITOR_API = env.NATS_MONITOR_API ?? '';
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

// The dead-subscription detector: JETSTREAM_EXPECTED_CONSUMERS (chart _helpers.tpl frontendEnv) is a
// comma list of "STREAM:service" rendered from the SAME values the Dapr pubsub components render, so
// the expectation cannot drift from what the estate actually subscribes. An expected group with no
// live consumer is INVISIBLE in raw /jsz — the app sits Ready while its trigger stream goes unread
// (the silent cascade stall, 2026-07-13) — so we diff expected vs present and return the gap.
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

// Per-consumer service label: Dapr's queueGroupName IS the subscriber app-id, so the deliver group
// names the service directly. The catalog control broadcast is deliberately group-less (every replica
// gets every event), so a no-group ephemeral on CATALOG_CONTROL is a catalog replica; any other
// group-less ephemeral (e.g. a nats-cli inspection consumer) gets the honest "(ephemeral)".
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
		console.error(`jetstream admin-gate upstream failure: ${String(err)}`);
		return { status: 503, detail: 'jetstream admin authorization is unavailable' };
	}
}

/** The trimmed JetStream overview the /streams panel renders. */
export const fetchJetstreamOverview = query(async (): Promise<ApiResult<JetStreamOverview>> => {
	const { fetch, locals } = getRequestEvent();
	if (locals.authEnabled && !locals.session) {
		return { ok: false, status: 401, detail: 'sign in to view JetStream streams' };
	}
	// Authorization (not just authentication): a governed, signed-in caller must be an ESTATE
	// admin — else any project's admin could map the estate's whole event fabric.
	if (locals.authEnabled && locals.session) {
		const denied = await estateAdminGate(fetch, locals.session.accessToken);
		if (denied) return { ok: false, status: denied.status, detail: denied.detail };
	}
	if (!NATS_MONITOR_API) {
		return { ok: false, status: 501, detail: 'jetstream monitor not configured' };
	}
	try {
		// Bounded: a wedged monitor (TCP accepted, no headers) must yield a fast honest 502, not pin
		// the request on undici's ~300s default. The caller's bearer rides along even though the real
		// monitor ignores every header — it is what lets the hermetic mock key its seeds per test,
		// and an unauthenticated in-cluster monitor cannot be confused by it.
		const res = await fetch(`${NATS_MONITOR_API}/jsz?streams=true&consumers=true&config=true`, {
			headers: locals.session
				? { authorization: `Bearer ${locals.session.accessToken}` }
				: undefined,
			signal: AbortSignal.timeout(5000),
		});
		if (!res.ok) {
			return { ok: false, status: 502, detail: `nats monitor ${res.status}` };
		}
		const raw = parse(RawJszSchema, await res.json());
		// Trim: flatten account_details[].stream_detail[] and keep only what the panel renders — the
		// browser never sees the raw monitor payload. Stream names are unique only PER ACCOUNT, so
		// with multiple accounts the name is account-qualified (the panel keys its each-block on it).
		const multiAccount = raw.account_details.length > 1;
		// Consumer groups per BARE stream name (expected/present matching must ignore the account
		// qualifier below), split by BOUNDNESS: a durable consumer outlives its subscriber, so mere
		// existence must not count as present (the dead-subscription false negative, audit
		// 2026-07-23). A consumer is bound when NATS says a push subscription is attached
		// (`push_bound`) or a pull client is actively waiting (`num_waiting` > 0). "*" marks a
		// group-less ephemeral on CATALOG_CONTROL — the catalog broadcast consumer carries no
		// deliver group by design, so ANY such (bound) consumer satisfies the expected entry.
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
		// Expected-vs-bound diff: an expected entry whose stream has no BOUND matching group (a
		// missing stream counts every expectation on it as missing) is a dead subscription. Order
		// follows the env list — the same order the Dapr components render in. When a consumer for
		// the group exists but nothing is attached (an orphaned durable), the entry is flagged
		// `unbound` so the panel can name the more deceptive flavor honestly.
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
		return { ok: true, data: overview };
	} catch (err) {
		// Fixed detail: a ValiError's message can echo received-value fragments of the raw monitor
		// payload — keep the specifics in the server log, never in the response.
		console.error(`jetstream upstream failure: ${String(err)}`);
		return {
			ok: false,
			status: 502,
			detail: 'jetstream monitor unreachable or its payload did not parse',
		};
	}
});
