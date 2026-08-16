// The mock NOTIFICATIONS service the watch-enrolment page reads through.
//
// `readWatches`/`watchProject`/`unwatchProject` are REMOTE functions: they run on the zone server and
// call `${RASK_GATEWAY_URL}/api/notifications/...` from there, where `page.route` cannot reach. Same
// reason mock-catalog and mock-observability exist, and the same reason a spec that tried to
// `page.route('**/api/notifications/**')` saw no effect at all — the request never touches the browser.
//
// DELIBERATELY NOT SEED-DRIVEN, unlike its two siblings. They stand in for upstreams whose RESPONSES
// are what a spec is testing (a 403 on the estate listing, a row of audit SQL). This one stands in for
// a registry whose contents are incidental: the watch surface's questions are "which projects can I
// see", "what happens when my identity is unresolvable", "does a controlplane outage break this page".
// A per-bearer seeding mechanism would be machinery for a fixture nobody varies. It keeps state in
// memory per bearer so a toggle round-trip still behaves like the real thing.

import { MOCK_NOTIFICATIONS_PORT } from './ports';

const watchesByBearer = new Map<string, Set<string>>();

const watchesOf = (bearer: string): Set<string> => {
	let set = watchesByBearer.get(bearer);
	if (!set) {
		set = new Set();
		watchesByBearer.set(bearer, set);
	}
	return set;
};

const json = (data: unknown, status = 200): Response =>
	new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

Bun.serve({
	port: MOCK_NOTIFICATIONS_PORT,
	fetch(req: Request): Response {
		const url = new URL(req.url);
		const bearer = (req.headers.get('authorization') ?? '').replace(/^Bearer /, '');

		if (url.pathname === '/api/notifications/watches' && req.method === 'GET') {
			// BOTH keys, exactly as `WatchListSchema` declares them (`{projects: string[], total: number}`).
			// `requestWatches` parses with valibot and swallows a parse failure into `null` — which the
			// page renders as "watching is unavailable on this stack", indistinguishable from a real
			// outage. A mock that is merely CLOSE therefore produces a green-looking red: this file's
			// first draft omitted `total` and every spec saw the un-wired card.
			const projects = [...watchesOf(bearer)];
			return json({ projects, total: projects.length });
		}

		const toggle = /^\/api\/notifications\/watches\/(.+)$/.exec(url.pathname);
		if (toggle) {
			const project = decodeURIComponent(toggle[1]);
			if (req.method === 'PUT') {
				watchesOf(bearer).add(project);
				return json({ project, watching: true });
			}
			if (req.method === 'DELETE') {
				watchesOf(bearer).delete(project);
				return json({ project, watching: false });
			}
		}

		// Everything else 404s, which every surface here renders as its honest "unavailable" state —
		// the same contract the sibling mocks hold to.
		return json({ detail: `unstubbed ${req.method} ${url.pathname}` }, 404);
	},
});
