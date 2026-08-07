// A tiny in-memory stand-in for the catalog endpoints the admin area reaches SERVER-SIDE, where
// page.route cannot reach: `GET /v1/events` (the control-events feed's query.live poll), `GET /v1/me`
// (the estate-admin door in admin/+layout.server.ts), the stores registry and `/v1/table`. Runs as a
// second Playwright `webServer`; the dev server's CATALOG_API points here. Test-control endpoints
// (`__mock/*`) seed events, simulate a governance mutation, toggle 403, and expose what the store
// writes received.
//
// The whole `/v1/access` surface LEFT with #105 — the FGA workbench is the home zone's
// `/settings/access` now, and its mock lives beside its spec there. What stayed is the store half of
// the same per-bearer ledger, because `attach-store.spec.ts` reads it.
//
// EVERYTHING mutable is keyed by the BEARER, never by shared server state: the suite is fullyParallel,
// so a spec that flipped a shared "current identity" — or read a shared request log — would race every
// other spec. session.ts mints the token per browser CONTEXT (the access spec appends a per-test
// suffix), so each test carries its own identity and nothing is shared.

import { ME_ADMIN, ME_MEMBER, TOKEN } from './session';
import { STORES } from './store-fixtures';
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

/** What one test's WRITES did — request shapes recorded per bearer, replacing the page.route capture
 *  variables the spec held while the transport was still browser-side. */
type AccessState = {
	/** Store drafts POSTed to /v1/stores by this bearer (the attach-store flow). */
	attachedStores: Body[];
	/** Set via `__mock/access/config`: the next store WRITE 403s — the partial-outcome / denied-form
	 *  lever (a failed write must be NAMED, never rolled into a fake success). */
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
		state = { attachedStores: [], failWrites: false };
		accessStates.set(bearer, state);
	}
	return state;
};

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

/** DEV-ONLY identity for a request that carries NO Authorization header.
 *
 *  Empty by default, so the contract every spec relies on is untouched: an absent bearer resolves to
 *  `''`, which `identityOf` does not know, and the mock 401s "exactly like the real catalog". It is set
 *  ONLY by `make dev-zone`, which runs the zone with auth OFF — no OIDC, no Dex, so the zone forwards no
 *  bearer and every seeded read would 401 into an empty state. Rather than teach the mock to accept an
 *  anonymous caller (that would trade a test's fidelity for a developer's convenience), the dev loop
 *  names an identity out of band and the mock adopts it only when told to.
 *
 *  Read once at module scope: this is process configuration, not per-request state. */
const DEV_BEARER = process.env.MOCK_DEV_BEARER ?? '';

Bun.serve({
	port: MOCK_CATALOG_PORT,
	async fetch(req: Request): Promise<Response> {
		const url = new URL(req.url);

		const bearer = (req.headers.get('authorization') ?? DEV_BEARER).replace(/^Bearer /, '');

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
				// GETs are recorded too, with a null body. They used to be skipped, which quietly made
				// every `expect(await callTo(…)).toBeUndefined()` on a read UNFALSIFIABLE — the ledger
				// could not have shown the call even if it fired. Reads are half the contract here
				// (the #75 recovery probe must NOT run for a live table), so they have to be visible.
				const body =
					req.method === 'GET' ? null : ((await req.json().catch(() => null)) as Body | null);
				callsOf(bearer).push({ method: req.method, path: url.pathname + url.search, body });
				const h = hit as { status?: unknown; body?: unknown };
				// NUMERIC status only — upstream payloads may carry their own `status` field (Prometheus
				// answers `status: "success"`) and must pass through as plain bodies.
				return h && typeof h === 'object' && typeof h.status === 'number'
					? json(h.body ?? {}, h.status)
					: json(hit);
			}
		}

		// ── the registry defaults (a spec that cares seeds its own; this keeps an unseeded read sane) ──
		if (url.pathname === '/v1/table') return json({ tables: ['db1$t'] });
		// #86 the estate-wide bindings read the namespaces page loads alongside the table list. Served
		// here because the page reaches it SERVER-side through a remote function, where page.route
		// cannot intercept — and an unserved endpoint would leave the page permanently in its
		// bindings-unavailable state, which is a different assertion than the specs mean to make.
		// EMPTY by default, deliberately: `ok` with no bindings keeps the degraded banner off (the
		// point of serving it at all) while adding no row to fixtures that never mentioned one. A
		// static `db1` here silently appended a phantom namespace to every other spec's world.
		if (url.pathname === '/v1/warehouses/-/bindings') return json({ bindings: {} });
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
		// ── test control plane ──
		// What the store writes RECEIVED, keyed by the caller's own bearer — the spec's replacement for
		// the capture variables page.route used to fill while the transport was browser-side. The name
		// is `__mock/access` for the same reason `AccessState` is: the failWrites lever is an
		// AUTHORIZATION lever, and attach-store is the one flow left that pulls it.
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
