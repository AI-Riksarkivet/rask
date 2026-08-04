/**
 * Local production serve for the Bun-server build (svelte-adapter-bun): run the
 * generated viewer-app request-handler AND reverse-proxy by domain to the three
 * lance-media services + the annotator zone, in ONE process. This mirrors the
 * rask topology (the MFE's Bun server serves the app; a gateway routes /api and
 * the sibling zones) — collapsed here into one thin server for local repro.
 *
 *   make frontend-build     # produces ./build (adapter-bun output)
 *   bun run server.ts --port 5274   # defaults: viewer :8101 search :8102 zone :5176
 *   # override any upstream: --viewer … --search … --annotate-zone …
 *
 * The Python services own Lance; this process serves the viewer app + forwards
 * /api/* per domain (HTTP Range preserved) and /annotator → the annotator zone.
 */

import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));

// ─── CLI args ────────────────────────────────────────────────────────────
const args = Object.fromEntries(
	process.argv
		.slice(2)
		.map((a, i, all) => (a.startsWith('--') ? [a.slice(2), all[i + 1]] : null))
		.filter((x): x is [string, string] => x !== null),
);
// Per-domain upstreams — the zone map, identical to the dev vite proxy. A real
// prod gateway would own this routing; the thin server keeps dev/prod parity so
// `bun run server.ts` serves the split stack end to end. Overridable via flags.
const VIEWER = (args.viewer ?? args.api ?? 'http://127.0.0.1:8101').replace(/\/$/, '');
const SEARCH = (args.search ?? 'http://127.0.0.1:8102').replace(/\/$/, '');
// The annotator ZONE app (owns /annotator) — its own bun server.
const ANNOTATE_ZONE = (args['annotate-zone'] ?? 'http://127.0.0.1:5176').replace(/\/$/, '');

/** Route an /api/* path to the service that owns that domain.
 *
 * The annotator branch is GONE, and its absence is the point: the workflow's tag write and its
 * batch-job submit are remote functions now (`src/lib/workflow/remote/labeling.remote.ts`), so they run
 * INSIDE the app handler below and reach `ANNOTATOR_API` from there. Nothing on `/api/*` in this zone
 * belongs to the annotator any more — proxying it here would be a write surface with no caller, which
 * is what the zone-contract gate exists to catch. */
function apiUpstream(pathname: string): string {
	if (pathname.startsWith('/api/search')) return SEARCH;
	return VIEWER;
}
const PORT = Number(args.port ?? 3000);

// ─── The adapter-bun build's request handler ───────────────────────────────
// (The old AUTH_GATE cookie-presence gate is gone: the build runs the real
// @rask/api/bff makeSessionHandle via hooks.server.ts — the login gate + session
// hydration are the app's own, identical to the estate zones.)
// Generated into ./build at build time; a dynamic import by absolute path keeps
// this file type-checkable/lintable without the build artifact present.
const { getHandler } = (await import(resolve(here, 'build/handler.js'))) as {
	getHandler: () => {
		fetch: (req: Request, server: unknown) => Response | Promise<Response>;
		websocket?: unknown;
	};
};
const app = getHandler();

// ─── streaming proxy to an explicit base (Range headers flow through) ─────────
// The workspace-patched svelte-adapter-bun serves a based zone's assets at the
// based path (/annotator/_app/…), so the annotator zone is forwarded verbatim —
// no base-stripping needed.
async function proxy(req: Request, base: string): Promise<Response> {
	const url = new URL(req.url);
	const pathname = url.pathname;
	const headers = new Headers(req.headers);
	headers.delete('host');
	const upstream = await fetch(`${base}${pathname}${url.search}`, {
		method: req.method,
		headers,
		body: req.method === 'GET' || req.method === 'HEAD' ? undefined : req.body,
	});
	// fetch() auto-decompresses the body, so the upstream's content-encoding /
	// content-length now describe bytes that no longer exist — forwarding them
	// makes the browser try to gunzip plain bytes (ERR_CONTENT_DECODING_FAILED).
	const respHeaders = new Headers(upstream.headers);
	respHeaders.delete('content-encoding');
	respHeaders.delete('content-length');
	return new Response(upstream.body, { status: upstream.status, headers: respHeaders });
}

// ─── Router: /api → per-domain service, /annotator → annotator zone, else app ──
Bun.serve({
	port: PORT,
	websocket: app.websocket as never,
	async fetch(req, server) {
		const { pathname } = new URL(req.url);
		if (pathname.startsWith('/api/')) return proxy(req, apiUpstream(pathname));
		if (pathname === '/annotator' || pathname.startsWith('/annotator/'))
			return proxy(req, ANNOTATE_ZONE);
		return app.fetch(req, server);
	},
});

console.log(`→ frontend:  http://localhost:${PORT}  (svelte-adapter-bun)`);
console.log(`  /api/search                  → ${SEARCH}`);
console.log(`  /api/*                       → ${VIEWER}`);
console.log(`  /annotator                   → ${ANNOTATE_ZONE}`);
