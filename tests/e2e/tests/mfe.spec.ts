import { test, expect } from '@playwright/test';

// Project-first URLs are HOST-based: the project is the request host and every MFE
// base path is `/<domain>` (the old `/default/<domain>` project segment is gone). So
// RASK_E2E_BASE_URL must point at a host that serves the domain apps — a provisioned
// project's URL, or a `singleTenant.enabled` install — NOT the front-door host (which
// serves only `/`, the picker). See docs/superpowers/specs/2026-06-29-openable-projects-design.md.
//
// Catch-all `/` (picker/landing) + each domain zone's real entry route (lakehouse has no index).
const ROUTES = [
	'/',
	'/lakehouse/data',
	'/media',
	'/annotator',
	'/compute',
	'/studio',
	'/train',
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

// Requires the fleet backend (the ray service) — i.e. a full/`singleTenant` deploy, not
// the front-door-only install where `/api/ray/health` has no upstream (gateway 502).
// (The core-api husk and its /api/health died in the R6/R20 wave; the ray service's
// own /health keeps the gateway round-trip probed.)
test('api round-trip via gateway returns 2xx (no internal-URL redirect)', async ({ request }) => {
	const res = await request.get('/api/ray/health', { maxRedirects: 0 });
	// 200 with data, OR a redirect whose Location is relative (never an absolute
	// internal address). A 3xx to http://127.0.0.1:8804/... is the bug.
	if (res.status() >= 300 && res.status() < 400) {
		const loc = res.headers()['location'] ?? '';
		expect(loc, 'redirect Location must be relative').not.toMatch(/^https?:\/\//);
	} else {
		expect(res.status()).toBeGreaterThanOrEqual(200);
		expect(res.status()).toBeLessThan(300);
	}
});
