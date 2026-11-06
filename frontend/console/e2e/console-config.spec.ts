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
	expect(res.headers()['content-security-policy']).toContain("frame-ancestors 'none'");
});
