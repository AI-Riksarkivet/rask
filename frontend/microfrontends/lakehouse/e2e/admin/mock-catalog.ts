// A tiny in-memory stand-in for the catalog endpoints the admin area reaches SERVER-SIDE, where
// page.route cannot reach: `GET /v1/events` (the control-events feed's query.live poll), `GET /v1/me`
// (the estate-admin door in admin/+layout.server.ts), and — since the remote-function migration — the
// whole `/v1/access` surface plus `/v1/table` (the FGA workbench's reads and writes run on the zone
// server through `access.remote.ts`). Runs as a second Playwright `webServer`; the dev server's
// CATALOG_API points here. Test-control endpoints (`__mock/*`) seed events, simulate a governance
// mutation, toggle 403, and expose what the access surface received.
//
// EVERYTHING mutable is keyed by the BEARER, never by shared server state: the suite is fullyParallel,
// so a spec that flipped a shared "current identity" — or read a shared request log — would race every
// other spec. session.ts mints the token per browser CONTEXT (the access spec appends a per-test
// suffix), so each test carries its own identity and nothing is shared.

import { ME_ADMIN, ME_MEMBER, TOKEN } from './session';
import {
	DSL,
	EXPAND_TREE,
	MODEL_CONDITIONS,
	STORES,
	TUPLES,
	type FixtureTuple,
} from './access-fixtures';
import { MOCK_CATALOG_PORT } from '../ports';

type ControlEvent = {
	event_id: string;
	occurred_at: string;
	action: string;
	object_type: string;
	object_id: string;
	actor: string | null;
	extra: Record<string, unknown>;
};

// Events and the 403 toggle are PER BEARER, like everything else mutable here. The bare `__mock/add`
// calls control-events.spec.ts makes (no bearer in the body) land in the TOKEN.admin bucket — the one
// its own signed-in server polls — so that file's serial discipline still holds within itself, while a
// spec that mints per-test bearers (access.spec.ts) can move ITS cursor without ever appearing in
// another file's feed. Before this split, the access live-update spec's `__mock/add` landed in the one
// global array and could make the console spec's "No recent changes." assertion flaky — or worse, pass
// it vacuously with a foreign event.
const eventsByBearer = new Map<string, ControlEvent[]>();
const modeByBearer = new Map<string, 'ok' | 'forbidden'>();
/** How many times each bearer's server has polled `/v1/events`. The live-update spec waits for ≥1
 *  before injecting its event: the control cursor SWALLOWS whatever its first successful probe sees
 *  (that read is the baseline, not a move — feeds.remote.ts), so an event injected before the first
 *  probe lands would be absorbed and the spec would time out flakily instead of testing the tick. */
const probesByBearer = new Map<string, number>();
const eventsOf = (bearer: string): ControlEvent[] => {
	let list = eventsByBearer.get(bearer);
	if (!list) {
		list = [];
		eventsByBearer.set(bearer, list);
	}
	return list;
};

/** The identity behind each e2e bearer. Unknown/absent token → 401, exactly like the real catalog.
 *  PREFIX-matched: the access spec signs in as `e2e-token:admin:<testId>` so its per-test state on
 *  this mock cannot race a parallel test's — same door, distinct ledger. */
const identityOf = (bearer: string): unknown => {
	if (bearer.startsWith(TOKEN.admin)) return ME_ADMIN;
	if (bearer.startsWith(TOKEN.member)) return ME_MEMBER;
	return undefined;
};

type Body = Record<string, unknown>;

/** What one test's access surface did — request shapes recorded per bearer, replacing the page.route
 *  capture variables the spec held while the transport was still browser-side. */
type AccessState = {
	extraTuples: FixtureTuple[];
	written: Body[];
	deleted: FixtureTuple[];
	expandBody: Body | null;
	listUsersBodies: Body[];
	listObjectsBody: Body | null;
	simulateBodies: Body[];
	/** Store drafts POSTed to /v1/stores by this bearer (the attach-store flow). */
	attachedStores: Body[];
	/** Set via `__mock/access/config`: the next WRITES (tuples, stores) 403 — the partial-outcome /
	 *  denied-form lever (a failed write must be NAMED, never rolled into a fake success). */
	failWrites: boolean;
};

/** GENERIC per-bearer surface for the transport-migrated specs: `__mock/seed` stores exact
 *  responses keyed by "METHOD /path" (with-query tried first, then without), `__mock/calls` returns
 *  every mutating request this bearer's server made. A spec seeds precisely the JSON its old
 *  page.route served — mechanical translation, no per-endpoint mock code, no shared mutable state. */
const seededByBearer = new Map<string, Map<string, unknown>>();
const callsByBearer = new Map<string, Body[]>();
const seededOf = (bearer: string): Map<string, unknown> => {
	let m = seededByBearer.get(bearer);
	if (!m) {
		m = new Map();
		seededByBearer.set(bearer, m);
	}
	return m;
};
const callsOf = (bearer: string): Body[] => {
	let list = callsByBearer.get(bearer);
	if (!list) {
		list = [];
		callsByBearer.set(bearer, list);
	}
	return list;
};

const accessStates = new Map<string, AccessState>();
const accessStateOf = (bearer: string): AccessState => {
	let state = accessStates.get(bearer);
	if (!state) {
		state = {
			extraTuples: [],
			written: [],
			deleted: [],
			expandBody: null,
			listUsersBodies: [],
			listObjectsBody: null,
			simulateBodies: [],
			attachedStores: [],
			failWrites: false,
		};
		accessStates.set(bearer, state);
	}
	return state;
};

const sameTuple = (a: FixtureTuple, b: FixtureTuple): boolean =>
	a.user === b.user && a.relation === b.relation && a.object === b.object;

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_CATALOG_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);

		const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');

		// The estate-admin door's identity lookup. `TOKEN.down` models a catalog outage, which must
		// fail CLOSED at the door rather than default open.
		if (url.pathname === '/v1/me') {
			if (bearer === TOKEN.down) return json({ detail: 'catalog unavailable' }, 502);
			const me = identityOf(bearer);
			return me ? json(me) : json({ detail: 'not authenticated' }, 401);
		}

		// The endpoint the query.live generator polls. since=N is a cursor == "events already seen"; we hand
		// back everything after it + the new head (== count), matching ControlEventBuffer.since semantics.
		// The generator polls with the signed-in session's bearer, which selects the bucket.
		if (url.pathname === '/v1/events') {
			if ((modeByBearer.get(bearer) ?? 'ok') === 'forbidden') {
				return json({ detail: 'not authorized' }, 403);
			}
			probesByBearer.set(bearer, (probesByBearer.get(bearer) ?? 0) + 1);
			const since = Number(url.searchParams.get('since') ?? '0');
			const list = eventsOf(bearer);
			return json({ events: list.slice(since), cursor: list.length, reset: false });
		}

		// ── the generic seeded surface (takes precedence: a seeding spec owns its bearer's world) ──
		if (!url.pathname.startsWith('/__mock/')) {
			const seeded = seededOf(bearer);
			const hit =
				seeded.get(`${req.method} ${url.pathname}${url.search}`) ??
				seeded.get(`${req.method} ${url.pathname}`);
			if (hit !== undefined) {
				if (req.method !== 'GET') {
					const body = (await req.json().catch(() => null)) as Body | null;
					callsOf(bearer).push({ method: req.method, path: url.pathname + url.search, body });
				}
				const h = hit as { status?: unknown; body?: unknown };
				// NUMERIC status only — upstream payloads may carry their own `status` field (Prometheus
				// answers `status: "success"`) and must pass through as plain bodies.
				return h && typeof h === 'object' && typeof h.status === 'number'
					? json(h.body ?? {}, h.status)
					: json(hit);
			}
		}

		// ── the /v1/access surface + the registry (the FGA workbench's remote functions land here) ──
		if (url.pathname === '/v1/table') return json({ tables: ['db1$t'] });
		// The stores registry (the storage area's remote functions). Reads are STATIC; the write is
		// per-bearer and failWrites-aware, echoing the whole registry exactly like the real catalog.
		if (url.pathname === '/v1/stores' && req.method === 'GET') return json({ stores: STORES });
		if (url.pathname === '/v1/stores/tiers') {
			const tiers: Record<string, unknown[]> = {};
			for (const s of STORES) (tiers[s.role] ??= []).push(s);
			return json(tiers);
		}
		if (url.pathname === '/v1/stores' && req.method === 'POST') {
			const state = accessStateOf(bearer);
			if (state.failWrites) return json({ detail: 'forbidden' }, 403);
			const body = (await req.json()) as Body;
			state.attachedStores.push(body);
			const attached = {
				name: body.name,
				bucket: body.bucket,
				role: body.role,
				description: body.description ?? '',
				read_only: true,
			};
			return json({ stores: [...STORES, attached] });
		}
		if (url.pathname === '/v1/access/model') {
			return json({ dsl: DSL, authorization_model_id: '01MODEL', conditions: MODEL_CONDITIONS });
		}
		if (url.pathname === '/v1/access/tuples') {
			const state = accessStateOf(bearer);
			if (req.method === 'GET') {
				const object = url.searchParams.get('object');
				let tuples = [...TUPLES, ...state.extraTuples].filter(
					(t) => !state.deleted.some((d) => sameTuple(d, t)),
				);
				if (object) tuples = tuples.filter((t) => t.object === object);
				return json({ tuples, continuation: null });
			}
			if (state.failWrites) return json({ detail: 'forbidden' }, 403);
			const body = (await req.json()) as FixtureTuple;
			if (req.method === 'POST') state.written.push(body as unknown as Body);
			if (req.method === 'DELETE') state.deleted.push(body);
			return json(body);
		}
		if (url.pathname === '/v1/access/check' && req.method === 'POST') {
			const body = (await req.json()) as Body;
			const user = String(body.user ?? '');
			return json({
				allowed: true,
				checked: {
					user: user.includes(':') ? user : `user:${user}`,
					relation: body.relation,
					object: body.object,
				},
			});
		}
		if (url.pathname === '/v1/access/expand' && req.method === 'POST') {
			accessStateOf(bearer).expandBody = (await req.json()) as Body;
			return json({
				tree: EXPAND_TREE,
				object: 'table:db1$t',
				relation: 'can_read_data',
				depth: 3,
			});
		}
		if (url.pathname === '/v1/access/list-users' && req.method === 'POST') {
			accessStateOf(bearer).listUsersBodies.push((await req.json()) as Body);
			return json({
				users: ['user:alice', 'user:bob', 'role:validators#assignee'],
				object: 'table:db1$t',
				relation: 'reader',
				user_type: 'user',
				user_relation: null,
				truncated: false,
			});
		}
		if (url.pathname === '/v1/access/list-objects' && req.method === 'POST') {
			accessStateOf(bearer).listObjectsBody = (await req.json()) as Body;
			return json({
				objects: ['table:db1$t', 'table:db1$u'],
				user: 'user:alice',
				relation: 'can_read_data',
				type: 'table',
			});
		}
		if (url.pathname === '/v1/access/simulate' && req.method === 'POST') {
			accessStateOf(bearer).simulateBodies.push((await req.json()) as Body);
			return json({
				allowed: true,
				baseline: false,
				checked: { user: 'user:carol', relation: 'reader', object: 'table:db1$t' },
				hypothetical: [{ user: 'user:carol', relation: 'reader', object: 'table:db1$t' }],
			});
		}

		// ── test control plane ──
		// What the access surface RECEIVED, keyed by the caller's own bearer — the spec's replacement for
		// the capture variables page.route used to fill while the transport was browser-side.
		if (url.pathname === '/__mock/access' && req.method === 'GET') {
			return json({ ...accessStateOf(bearer), eventProbes: probesByBearer.get(bearer) ?? 0 });
		}
		// A tuple landing from ELSEWHERE (another admin, the grant API, a bootstrap job): visible only to
		// the given bearer's reads, so a parallel test's canvas cannot inherit it. Pair with `__mock/add`
		// to move the control cursor — landing a grant AND announcing it is exactly what the real catalog
		// does now (access_admin emits `grant_added` on every raw-tuple write).
		if (url.pathname === '/__mock/seed' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; routes: Record<string, unknown> };
			const seeded = seededOf(body.bearer);
			for (const [key, value] of Object.entries(body.routes)) seeded.set(key, value);
			return json({ ok: true, seeded: seeded.size });
		}
		if (url.pathname === '/__mock/calls' && req.method === 'GET') {
			return json({ calls: callsOf(bearer) });
		}
		if (url.pathname === '/__mock/access/config' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; failWrites?: boolean };
			accessStateOf(body.bearer).failWrites = body.failWrites ?? false;
			return json({ ok: true });
		}
		if (url.pathname === '/__mock/access/tuple' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; tuple: FixtureTuple };
			accessStateOf(body.bearer).extraTuples.push(body.tuple);
			return json({ ok: true });
		}
		if (url.pathname === '/__mock/add' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as Partial<ControlEvent> & {
				bearer?: string;
			};
			const list = eventsOf(body.bearer ?? TOKEN.admin);
			list.push({
				event_id: `evt-${list.length + 1}`,
				occurred_at: '2026-07-23T10:30:00Z',
				action: body.action ?? 'grant_added',
				object_type: body.object_type ?? 'grant',
				object_id: body.object_id ?? 'table:db1$t',
				actor: body.actor ?? 'user:alice',
				extra: body.extra ?? {},
			});
			return json({ ok: true, cursor: list.length });
		}
		if (url.pathname === '/__mock/mode' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as { mode?: string; bearer?: string };
			const next = body.mode === 'forbidden' ? ('forbidden' as const) : ('ok' as const);
			modeByBearer.set(body.bearer ?? TOKEN.admin, next);
			return json({ ok: true, mode: next });
		}
		if (url.pathname === '/__mock/reset' && req.method === 'POST') {
			// Resets ONE bucket (the caller's own scope) — a global wipe would race every other worker.
			const body = (await req.json().catch(() => ({}))) as { bearer?: string };
			const bucket = body.bearer ?? TOKEN.admin;
			eventsOf(bucket).length = 0;
			modeByBearer.set(bucket, 'ok');
			return json({ ok: true });
		}
		return json({ detail: 'not found' }, 404);
	},
});
