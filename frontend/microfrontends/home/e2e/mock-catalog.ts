// A tiny in-memory stand-in for the CATALOG endpoints this zone reaches SERVER-SIDE, where
// `page.route` cannot reach: `GET /v1/me` (this zone's `+layout.server.ts` calls `fetchMe` against
// CATALOG_API directly — there is no `/capi/v1/me` browser hop to intercept the way the lakehouse zone
// had), `GET /v1/projects` (the estate listing, via this zone's own `/capi` pass-through),
// `GET /v1/projects/<p>` (the overview's `fetchProject` query), `POST /v1/warehouses` (the create that
// MINTS a project) and `POST /v1/access/tuples` (the initial-admin grant) — plus, since #105, the whole
// `/v1/access` surface and the control-event feed that the PLATFORM settings pages read
// (`/settings/access`, `/settings/audit`). Runs as a second Playwright `webServer`; the auth-ON dev
// server's CATALOG_API points here.
//
// The mechanism is the lakehouse mock catalog's GENERIC one, kept per-BEARER: `__mock/seed` stores
// exact responses under "METHOD /path" (with-query tried first, then without) and `__mock/calls`
// returns every mutating request that bearer's app server made. A spec seeds precisely the JSON it
// wants the catalog to answer — no per-endpoint mock code, no shared mutable state. There is no
// The settings suite needs more than seeds, and says why: the FGA canvas reads a STORE it also writes
// to, so its assertions are about what the surface RECEIVED (`__mock/access`) and about a tuple landing
// from elsewhere (`__mock/access/tuple`) — neither of which a static seed can express. Those handlers
// moved here verbatim from the lakehouse's mock with the workbench itself. For the projects surface
// there is still no separate failure lever: a denied write is just a seeded `{status: 403}`.
//
// EVERYTHING is keyed by the BEARER, never by shared server state: the suite is fullyParallel, so a
// spec that flipped a shared "current identity" — or read a shared request log — would race every
// other spec. session.ts mints the token per browser CONTEXT with the test's own id appended, so each
// test carries its own identity and nothing is shared.

import { MOCK_CATALOG_PORT } from './ports';
import {
	DSL,
	EXPAND_TREE,
	MODEL_CONDITIONS,
	TUPLES,
	type FixtureTuple,
} from './settings/access-fixtures';

type Body = Record<string, unknown>;

type ControlEvent = {
	event_id: string;
	occurred_at: string;
	action: string;
	object_type: string;
	object_id: string;
	actor: string | null;
	extra: Record<string, unknown>;
};

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
	/** Set via `__mock/access/config`: the next write 403s — the denied-form lever (a failed write must
	 *  be NAMED, never rolled into a fake success). */
	failWrites: boolean;
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
			failWrites: false,
		};
		accessStates.set(bearer, state);
	}
	return state;
};

const sameTuple = (a: FixtureTuple, b: FixtureTuple): boolean =>
	a.user === b.user && a.relation === b.relation && a.object === b.object;

/** Control events, per bearer like everything mutable here — a spec that moves ITS cursor must never
 *  appear in a parallel spec's feed. */
const eventsByBearer = new Map<string, ControlEvent[]>();
const eventsOf = (bearer: string): ControlEvent[] => {
	let list = eventsByBearer.get(bearer);
	if (!list) {
		list = [];
		eventsByBearer.set(bearer, list);
	}
	return list;
};
/** How many times each bearer's server has polled `/v1/events`. The live-update spec waits for >=1
 *  before injecting its event: the control cursor SWALLOWS whatever its first successful probe sees
 *  (that read is the baseline, not a move — feeds.remote.ts), so an event injected before the first
 *  probe lands would be absorbed and the spec would time out flakily instead of testing the tick. */
const probesByBearer = new Map<string, number>();

/** Exact responses a spec seeded for ITS bearer, keyed "METHOD /path" (query included or not). */
const seededByBearer = new Map<string, Map<string, unknown>>();
/** Every non-GET that bearer's app server made — the ledger a spec reads back to pin wire bodies. */
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

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_CATALOG_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);
		const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');

		// ── test control plane ──
		if (url.pathname === '/__mock/seed' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; routes: Record<string, unknown> };
			const seeded = seededOf(body.bearer);
			for (const [key, value] of Object.entries(body.routes)) seeded.set(key, value);
			return json({ ok: true, seeded: seeded.size });
		}
		if (url.pathname === '/__mock/calls' && req.method === 'GET') {
			return json({ calls: callsOf(bearer) });
		}
		// No `__mock/reset` (the lakehouse and annotator mocks have one): a bearer is minted per TEST, so
		// every bucket here is already single-use and a reset would have no state to clear. An endpoint
		// no spec drives is test infrastructure nothing verifies.

		if (url.pathname === '/__mock/access' && req.method === 'GET') {
			return json({ ...accessStateOf(bearer), eventProbes: probesByBearer.get(bearer) ?? 0 });
		}
		if (url.pathname === '/__mock/access/config' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; failWrites?: boolean };
			accessStateOf(body.bearer).failWrites = body.failWrites ?? false;
			return json({ ok: true });
		}
		// A tuple landing from ELSEWHERE (another admin, the grant API, a bootstrap job): visible only to
		// the given bearer's reads, so a parallel test's canvas cannot inherit it. Pair with `__mock/add`
		// to move the control cursor — landing a grant AND announcing it is exactly what the real catalog
		// does (access_admin emits `grant_added` on every raw-tuple write).
		if (url.pathname === '/__mock/access/tuple' && req.method === 'POST') {
			const body = (await req.json()) as { bearer: string; tuple: FixtureTuple };
			accessStateOf(body.bearer).extraTuples.push(body.tuple);
			return json({ ok: true });
		}
		if (url.pathname === '/__mock/add' && req.method === 'POST') {
			const body = (await req.json().catch(() => ({}))) as Partial<ControlEvent> & {
				bearer?: string;
			};
			const list = eventsOf(body.bearer ?? '');
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

		// ── the control-plane cursor + the audit gate probe ──
		// `since=N` is a cursor == "events already seen"; hand back everything after it plus the new head
		// (== count), matching ControlEventBuffer.since semantics. `feeds.remote.ts`'s generator polls with
		// the signed-in session's bearer, which selects the bucket. The audit trail's estate-admin gate
		// probes the SAME endpoint with a past-any-head cursor, so an unknown bearer must 403 here or the
		// gate would default open.
		if (url.pathname === '/v1/events') {
			if (!bearer) return json({ detail: 'not authenticated' }, 401);
			probesByBearer.set(bearer, (probesByBearer.get(bearer) ?? 0) + 1);
			const since = Number(url.searchParams.get('since') ?? '0');
			const list = eventsOf(bearer);
			return json({ events: list.slice(since), cursor: list.length, reset: false });
		}

		// ── the /v1/access surface (the FGA workbench's remote functions land here) ──
		// A seed for the same path still wins — it is checked below, and a seeding spec owns its bearer's
		// world — but these defaults are what let the workbench spec assert a WORKFLOW rather than
		// re-state the catalog on every test.
		const seededHere = seededOf(bearer);
		const isSeeded =
			seededHere.has(`${req.method} ${url.pathname}${url.search}`) ||
			seededHere.has(`${req.method} ${url.pathname}`);
		if (!isSeeded && url.pathname.startsWith('/v1/access/')) {
			const state = accessStateOf(bearer);
			if (url.pathname === '/v1/access/model') {
				return json({ dsl: DSL, authorization_model_id: '01MODEL', conditions: MODEL_CONDITIONS });
			}
			if (url.pathname === '/v1/access/tuples') {
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
				state.expandBody = (await req.json()) as Body;
				return json({
					tree: EXPAND_TREE,
					object: 'table:db1$t',
					relation: 'can_read_data',
					depth: 3,
				});
			}
			if (url.pathname === '/v1/access/list-users' && req.method === 'POST') {
				state.listUsersBodies.push((await req.json()) as Body);
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
				state.listObjectsBody = (await req.json()) as Body;
				return json({
					objects: ['table:db1$t', 'table:db1$u'],
					user: 'user:alice',
					relation: 'can_read_data',
					type: 'table',
				});
			}
			if (url.pathname === '/v1/access/simulate' && req.method === 'POST') {
				state.simulateBodies.push((await req.json()) as Body);
				return json({
					allowed: true,
					baseline: false,
					checked: { user: 'user:carol', relation: 'reader', object: 'table:db1$t' },
					hypothetical: [{ user: 'user:carol', relation: 'reader', object: 'table:db1$t' }],
				});
			}
		}
		// The catalog registry the workbench offers as one-click graph seeds. A spec that cares seeds it;
		// this keeps an unseeded read from rendering the honest-but-irrelevant "registry unavailable".
		if (!isSeeded && url.pathname === '/v1/table' && req.method === 'GET') {
			return json({ tables: ['db1$t'] });
		}

		// ── the seeded surface ──
		// Recorded BEFORE the lookup, hit or miss: "no tuple was ever written" is only a real assertion
		// if a write the spec never seeded would still show up in the ledger.
		if (req.method !== 'GET') {
			const body = (await req.json().catch(() => null)) as Body | null;
			callsOf(bearer).push({ method: req.method, path: url.pathname + url.search, body });
		}
		const seeded = seededOf(bearer);
		const hit =
			seeded.get(`${req.method} ${url.pathname}${url.search}`) ??
			seeded.get(`${req.method} ${url.pathname}`);
		if (hit === undefined) {
			// An unseeded identity is the catalog's own answer to an unknown bearer, and the zone must
			// degrade on it (navbar signed-out, gallery told nothing) rather than 500.
			return url.pathname === '/v1/me'
				? json({ detail: 'not authenticated' }, 401)
				: json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
		}
		const shaped = hit as { status?: unknown; body?: unknown };
		// NUMERIC status only — an upstream payload carries its own `status` field (a warehouse record
		// answers `status: "active"`, Prometheus answers `status: "success"`), and a STRING one must pass
		// through as a plain body. Keying on the key's presence made `"active"` the HTTP status.
		return shaped && typeof shaped === 'object' && typeof shaped.status === 'number'
			? json(shaped.body ?? {}, shaped.status)
			: json(hit);
	},
});
