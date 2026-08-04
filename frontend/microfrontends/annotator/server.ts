/**
 * Local production serve for the annotator zone (svelte-adapter-bun). Serves the
 * base-pathed app (/annotator) AND reverse-proxies /api/* per domain to the three
 * lance-media services — the same zone map as the viewer server, minus the
 * /annotator route (this IS the annotator zone). The viewer origin proxies
 * /annotator here locally; in-cluster the lance ingress owns the routing.
 *
 *   bun run build && bun run server.ts --port 5176
 */
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const args = Object.fromEntries(
	process.argv
		.slice(2)
		.map((a, i, all) => (a.startsWith('--') ? [a.slice(2), all[i + 1]] : null))
		.filter((x): x is [string, string] => x !== null),
);
const VIEWER = (args.viewer ?? 'http://127.0.0.1:8101').replace(/\/$/, '');
const ANNOTATOR = (args.annotator ?? 'http://127.0.0.1:8103').replace(/\/$/, '');
const PORT = Number(args.port ?? 5176);

// (The old AUTH_GATE cookie-presence gate is gone: the build runs the real
// @rask/api/bff makeSessionHandle via hooks.server.ts — the login gate + session
// hydration are the app's own, identical to the estate zones.)

// Only `/api/annotations` still leaves the app as a BFF route — the assist and jobs planes became
// remote functions (the transport ruling, area 4), which are app endpoints under `/annotator/_app/…`
// and are served by the handler below, never proxied.
function apiUpstream(pathname: string): string {
	return pathname.startsWith('/api/annotations') ? ANNOTATOR : VIEWER;
}

const { getHandler } = (await import(resolve(here, 'build/handler.js'))) as {
	getHandler: () => {
		fetch: (req: Request, server: unknown) => Response | Promise<Response>;
		websocket?: unknown;
	};
};
const app = getHandler();

async function proxy(req: Request, base: string): Promise<Response> {
	const url = new URL(req.url);
	const headers = new Headers(req.headers);
	headers.delete('host');
	const upstream = await fetch(`${base}${url.pathname}${url.search}`, {
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

// The workspace-patched svelte-adapter-bun serves this based app's assets at
// the based path (/annotator/_app/…) like its pages, so requests pass through
// verbatim — no asset-base rewriting.
Bun.serve({
	port: PORT,
	websocket: app.websocket as never,
	async fetch(req, server) {
		const { pathname } = new URL(req.url);
		if (pathname.startsWith('/api/')) return proxy(req, apiUpstream(pathname));
		return app.fetch(req, server);
	},
});
console.log(`→ annotator zone:  http://localhost:${PORT}/annotator`);
