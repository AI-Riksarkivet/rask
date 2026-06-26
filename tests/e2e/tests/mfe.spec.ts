import { test, expect } from '@playwright/test';

// Catch-all + each domain app's real entry route (discover has no index).
const ROUTES = [
	'/',
	'/default/overview',
	'/default/storage',
	'/default/compute',
	'/default/discover/browse',
	'/default/discover/search',
	'/default/train',
	'/default/studio',
];

for (const route of ROUTES) {
	test(`hydrates: ${route}`, async ({ page }) => {
		const appAsset404 = [];
		const pageErrors = [];
		page.on('requestfailed', (r) => {
			if (r.url().includes('/_app/')) appAsset404.push(r.url());
		});
		page.on('response', (r) => {
			if (r.url().includes('/_app/') && r.status() >= 400) appAsset404.push(r.url());
		});
		page.on('pageerror', (e) => pageErrors.push(String(e)));
		const resp = await page.goto(route, { waitUntil: 'networkidle' });
		expect(resp?.status(), `status for ${route}`).toBe(200);
		await page.waitForTimeout(800);
		expect(appAsset404, `failed _app assets on ${route}`).toEqual([]);
		expect(pageErrors, `page errors on ${route}`).toEqual([]);
	});
}

test('api round-trip via gateway returns 2xx (no internal-URL redirect)', async ({ request }) => {
	const res = await request.get('/api/batches/', { maxRedirects: 0 });
	// 200 with data, OR a redirect whose Location is relative (never an absolute
	// internal address). A 3xx to http://127.0.0.1:8801/... is the bug.
	if (res.status() >= 300 && res.status() < 400) {
		const loc = res.headers()['location'] ?? '';
		expect(loc, 'redirect Location must be relative').not.toMatch(/^https?:\/\//);
	} else {
		expect(res.status()).toBeGreaterThanOrEqual(200);
		expect(res.status()).toBeLessThan(300);
	}
});
