/**
 * The console's static server: the built single-page app, its runtime config, and its security headers.
 * Nothing else — no proxy, no session, no credential; the browser calls the APIs itself.
 *
 *  - `build/` is served as files; any other path answers `index.html`, which routes in the browser —
 *    except `/api/*`, which answers 404: the Gateway routes those to their services, so one reaching
 *    this server names a route nothing serves, and a 200 of HTML would hide it.
 *  - `/config.json` is the file at `$CONSOLE_CONFIG_FILE` (the release mounts it from its values), so
 *    the image carries no environment.
 *  - The content security policy is the one SvelteKit wrote into `index.html` at build (with the hash
 *    of its inline bootstrap script). It is sent as a HEADER instead, with the issuer's origin added to
 *    `connect-src` when the issuer is not this origin, and `frame-ancestors 'none'`, which a meta tag
 *    cannot carry. The meta tag is removed: a meta policy and a header policy both apply, so a meta
 *    `connect-src 'self'` would block a cross-origin issuer whatever the header allowed.
 *  - `/readyz` answers the kubelet's readiness probe. The server depends on nothing but the files it
 *    read at start, so serving at all is being ready.
 *  - On SIGTERM it stops accepting, answers the requests in flight, and exits 0. `server.stop()`
 *    with no argument waits for them: measured 2026-11-17 on Bun 1.4.2, it held for a 1.5 s request,
 *    which completed with its body, and closed an idle keep-alive socket at once.
 */
import { join, normalize } from 'node:path';

const ROOT = join(import.meta.dir, 'build');
const PORT = Number(process.env.PORT ?? 3000);
const CONFIG_FILE = process.env.CONSOLE_CONFIG_FILE ?? '';

const META_CSP = /<meta http-equiv="content-security-policy" content="([^"]*)">/;

const rawIndex = await Bun.file(join(ROOT, 'index.html')).text();
const builtPolicy = META_CSP.exec(rawIndex)?.[1] ?? "default-src 'self'";
const index = rawIndex.replace(META_CSP, '');
const config = CONFIG_FILE ? await Bun.file(CONFIG_FILE).text() : null;

/** The issuer's origin, when the config names one. */
function issuerOrigin(): string | null {
	if (!config) return null;
	try {
		const issuer: unknown = (JSON.parse(config) as { issuer?: unknown }).issuer;
		return typeof issuer === 'string' ? new URL(issuer).origin : null;
	} catch {
		return null;
	}
}

function policy(): string {
	const origin = issuerOrigin();
	const directives = builtPolicy
		.split(';')
		.map((d) => d.trim())
		.filter(Boolean)
		.map((d) => (origin && d.startsWith('connect-src ') ? `${d} ${origin}` : d));
	return [...directives, "frame-ancestors 'none'"].join('; ');
}

// HSTS is read only over https, where it tells the browser never to ask for this host in the clear
// again; over http a browser ignores it. It names this host alone: whether its subdomains serve https
// is the platform's to say, at the Gateway that terminates TLS. The opener policy puts the console in a browsing context group
// of its own, so a page that opened it, or that it opened on another origin, holds no handle on its
// window; the workbench's popouts are same-origin and keep theirs.
const SECURITY_HEADERS = {
	'content-security-policy': policy(),
	'x-content-type-options': 'nosniff',
	'referrer-policy': 'no-referrer',
	'strict-transport-security': 'max-age=31536000',
	'cross-origin-opener-policy': 'same-origin',
};

function page(body: string, contentType: string, cache: string): Response {
	return new Response(body, {
		headers: { ...SECURITY_HEADERS, 'content-type': contentType, 'cache-control': cache },
	});
}

const server = Bun.serve({
	hostname: '0.0.0.0',
	port: PORT,
	async fetch(request) {
		const { pathname } = new URL(request.url);
		if (pathname === '/readyz') {
			return Response.json(
				{ status: 'ready' },
				{ headers: { ...SECURITY_HEADERS, 'cache-control': 'no-store' } },
			);
		}
		if (pathname === '/api' || pathname.startsWith('/api/')) {
			return Response.json(
				{ detail: `no route serves ${pathname}` },
				{
					status: 404,
					headers: SECURITY_HEADERS,
				},
			);
		}
		if (pathname === '/config.json') {
			if (!config) return new Response('no config', { status: 404, headers: SECURITY_HEADERS });
			return page(config, 'application/json', 'no-store');
		}
		// A malformed percent-escape names no file and no route: the request is the client's error.
		let decoded: string;
		try {
			decoded = decodeURIComponent(pathname);
		} catch {
			return new Response('malformed percent-escape in the path', {
				status: 400,
				headers: SECURITY_HEADERS,
			});
		}
		const path = normalize(join(ROOT, decoded));
		if (path.startsWith(ROOT + '/') && !path.endsWith('/index.html')) {
			const file = Bun.file(path);
			if (await file.exists()) {
				// Hashed assets never change under their name; everything else is re-checked.
				const immutable = pathname.startsWith('/_app/immutable/');
				return new Response(file, {
					headers: {
						...SECURITY_HEADERS,
						'cache-control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache',
					},
				});
			}
		}
		return page(index, 'text/html; charset=utf-8', 'no-cache');
	},
});

process.on('SIGTERM', () => {
	void server.stop().then(() => process.exit(0));
});

console.log(`console serving ${ROOT} on :${PORT}`);
