// The e2e stand-in for the platform's Gateway: one origin that routes each `/api/*` prefix to its
// (mock) service, as a deployment's HTTPRoutes do, and everything else to the console's own static
// server, `serve.ts` — the very server the image ships, so its CSP and SPA fallback are what the specs
// meet. Bun-native on purpose: Bun's `node:http` emulation is not reliable under a Node HTTP proxy.
//
//   PORT                the origin the browser uses
//   CATALOG_API         /api/catalog  → its root paths (the prefix stripped)
//   LINEAGE_API         /api/lineage  → its root paths (the Gateway's own rewrite)
//   NOTIFICATIONS_API   /api/notifications  (kept: the inbox serves the prefix itself)
//   OIDC_MOCK           /oidc → the mock issuer, told this origin's host in `x-forwarded-host`
//   CONSOLE_CONFIG_FILE passed through to serve.ts (absent: /config.json 404s and the console refuses to run)
//   INNER is PORT + 1000, where serve.ts listens behind this edge.

const PORT = Number(process.env.PORT);
const INNER = PORT + 1000;

const env = process.env;
const routes: { prefix: string; target: string | undefined; strip: boolean }[] = [
	{ prefix: '/api/catalog', target: env.CATALOG_API, strip: true },
	{ prefix: '/api/lineage', target: env.LINEAGE_API, strip: true },
	{ prefix: '/api/notifications', target: env.NOTIFICATIONS_API, strip: false },
	{ prefix: '/oidc', target: env.OIDC_MOCK, strip: true },
];

Bun.spawn(['bun', 'serve.ts'], {
	cwd: new URL('..', import.meta.url).pathname,
	env: { ...process.env, PORT: String(INNER) },
	stdout: 'inherit',
	stderr: 'inherit',
});
for (let tries = 0; ; tries++) {
	const up = await fetch(`http://127.0.0.1:${INNER}/`).then(
		(r) => r.ok,
		() => false,
	);
	if (up) break;
	if (tries > 100) throw new Error('serve.ts did not start');
	await Bun.sleep(50);
}

/** Response headers that describe bytes `fetch` has already decoded, or the hop itself. */
const DROPPED = ['content-encoding', 'content-length', 'transfer-encoding', 'connection'];

Bun.serve({
	port: PORT,
	async fetch(req) {
		const url = new URL(req.url);
		const match = routes.find(
			(r) => r.target && (url.pathname === r.prefix || url.pathname.startsWith(`${r.prefix}/`)),
		);
		const path = match?.strip ? url.pathname.slice(match.prefix.length) || '/' : url.pathname;
		const base = match ? match.target! : `http://127.0.0.1:${INNER}`;
		const headers = new Headers(req.headers);
		headers.set('x-forwarded-host', url.host);
		headers.delete('host');
		let upstream: Response;
		try {
			upstream = await fetch(`${base}${path}${url.search}`, {
				method: req.method,
				headers,
				body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.arrayBuffer(),
				redirect: 'manual',
			});
		} catch (err) {
			return Response.json({ detail: `upstream unreachable: ${String(err)}` }, { status: 502 });
		}
		const out = new Headers(upstream.headers);
		for (const name of DROPPED) out.delete(name);
		return new Response(upstream.body, { status: upstream.status, headers: out });
	},
});

console.log(`e2e edge on :${PORT} (console on :${INNER})`);
