import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';

import { test, expect } from '@playwright/test';

// The console's own static server and its runtime config, as the image ships them (`serve.ts` behind
// the e2e edge). A config the console cannot use FAILS CLOSED: it runs nothing and says why, rather
// than quietly signing nobody in. Sign-in is off only where the config says `{"signIn": false}`.

for (const { name, answer, why } of [
	{
		name: 'missing',
		answer: { status: 404, body: 'no config' },
		why: '/config.json answered 404',
	},
	{
		name: 'malformed',
		answer: { status: 200, body: '{"issuer": "/oidc"}', contentType: 'application/json' },
		why: '/config.json is malformed',
	},
]) {
	test(`a ${name} /config.json is refused: the console runs nothing and says why`, async ({
		page,
	}) => {
		const catalog: string[] = [];
		page.on('request', (req) => {
			if (new URL(req.url()).pathname.startsWith('/api/')) catalog.push(req.url());
		});
		await page.route('**/config.json', (route) => route.fulfill(answer));

		await page.goto('/');

		await expect(page.getByText(`Sign-in is not configured: ${why}`)).toBeVisible();
		await expect(page.getByText('503')).toBeVisible();
		expect(catalog).toEqual([]);
	});
}

test('a malformed percent-escape in the path is a 400, not a server error', async ({ request }) => {
	const res = await request.get('/catalog/%E0%A4%A');
	expect(res.status()).toBe(400);
	const sent = res.headers();
	expect(sent['content-security-policy']).toContain("frame-ancestors 'none'");
	// Every answer carries the policies, an error's too: no http after one https visit, and no handle on
	// the console's window for a page on another origin.
	expect([sent['strict-transport-security'], sent['cross-origin-opener-policy']]).toEqual([
		'max-age=31536000',
		'same-origin',
	]);
});

test('the server answers /readyz as ready, and told to stop it exits cleanly', async () => {
	const probe = createServer().listen(0, '127.0.0.1');
	await once(probe, 'listening');
	const { port } = probe.address() as { port: number };
	probe.close();
	const server = spawn('bun', [fileURLToPath(new URL('../serve.ts', import.meta.url))], {
		cwd: fileURLToPath(new URL('..', import.meta.url)),
		env: { ...process.env, PORT: String(port) },
		stdio: 'ignore',
	});
	try {
		// What the kubelet's readiness probe reads: JSON saying ready, never the app's page.
		await expect
			.poll(() =>
				fetch(`http://127.0.0.1:${port}/readyz`).then(
					async (r) => [r.status, r.headers.get('content-type'), await r.text()],
					() => null,
				),
			)
			.toEqual([200, 'application/json;charset=utf-8', '{"status":"ready"}']);

		server.kill('SIGTERM');
		const [code, signal] = (await once(server, 'exit')) as [number | null, string | null];

		expect({ code, signal }).toEqual({ code: 0, signal: null });
	} finally {
		server.kill('SIGKILL');
	}
});
