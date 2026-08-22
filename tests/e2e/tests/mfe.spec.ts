import { test, expect } from '@playwright/test';

// Project-first URLs are HOST-based: the project is the request host and every MFE
// base path is `/<domain>` (the old `/default/<domain>` project segment is gone). So
// RASK_E2E_BASE_URL must point at a host that serves the domain apps — a provisioned
// project's URL, or a `singleTenant.enabled` install — NOT the front-door host (which
// serves only `/`, the picker). See docs/superpowers/specs/2026-06-29-openable-projects-design.md.
//
// Catch-all `/` + every zone's root. Keep this in step with `git ls-files frontend/microfrontends |
// cut -d/ -f3 | sort -u` — the roster is the same one `@rask/zone-contract`'s manifest test pins.
//
// It had DRIFTED (audit M3): it listed `/media` and `/train`, neither of which is a zone. `models`
// REPLACED `train` (on train's own port) and `media` was never one — a leftover
// `frontend/microfrontends/media/` on a dev host is untracked build residue. Both 404 against the
// live deploy, so the two of them were the only routes here that could fail, and they would have
// failed for being wrong rather than for finding anything. `explorer` and `models` — two zones that
// DO exist — were not covered at all.
//
// TRAILING SLASH on the zone roots, deliberately: measured against the live ingress, `/compute`
// answers 308 and `/compute/` answers 200 directly. `page.goto` follows the redirect either way, so
// the bare form still passes — it just spends a round trip per zone and hides which form the
// ingress actually serves. Same rule the sidebar gate enforces for cross-zone links.
//
// `/lakehouse/` rather than `/lakehouse/catalog`: since #109 the zone root IS the overview, and
// `/lakehouse/catalog` is itself a 307 to `/lakehouse/catalog/tables`.
const ROUTES = [
	'/',
	'/lakehouse/',
	'/explorer/',
	'/annotator/',
	'/compute/',
	'/models/',
	'/studio/',
];

// A RUN THAT COVERED NOTHING MUST NOT EXIT 0.
//
// Every route test below correctly SKIPS when the deploy bounces it to auth — an untested surface must
// not be indistinguishable from a passing one, which is audit M3's fix and stays. What that fix could
// not do is speak for the RUN: the chart has shipped `auth.enabled: true` by default since 2026-08-06,
// so on any default estate all seven skip, `make e2e` exits 0, and the estate reports a green browser
// suite that exercised no zone at all. Seven honest skips still add up to a dishonest run.
//
// So the reachability of the target is asserted ONCE, as a failure. Skipping is the right answer for
// "this particular route was not covered"; it is the wrong answer for "nothing was covered and nobody
// will be told". The remedy is in the message rather than in a comment, because the previous version
// offered a remedy that did not exist.
test('the deploy under test is reachable and not auth-gated', async ({ page }) => {
	const resp = await page.goto('/', { waitUntil: 'domcontentloaded' });
	const landed = new URL(page.url());
	const target = new URL('/', resp?.url() ?? page.url());
	const bouncedToAuth =
		landed.origin !== target.origin || /\/dex\/|\/oauth2\/|\/auth\/(login|callback)/.test(landed.pathname);

	expect(
		bouncedToAuth,
		`the run bounced to ${page.url()}, so every route test below will skip and this suite will ` +
			`report success having exercised no zone. Either point RASK_E2E_BASE_URL at an auth-off ` +
			`install, or set RASK_E2E_STORAGE_STATE to a signed-in Playwright storage state (see ` +
			`playwright.config.ts). This is a FAILURE and not a skip because a run that covered nothing ` +
			`must not be indistinguishable from one that covered everything.`,
	).toBe(false);
});

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
		// `domcontentloaded`, NOT `networkidle`: every zone's shell holds a live `query.live` stream open
		// for the notification bell, so these apps have no idle network by design — the wait sits until its
		// own timeout and then reports the product as hanging. The asset/error assertions below do not need
		// an idle network anyway; they need the page to have loaded and settled, which is what the explicit
		// `waitForTimeout` after them is for. Pinned by @rask/zone-contract's no-networkidle gate, which
		// until 2026-08-22 scanned only `microfrontends/<zone>/e2e` and could not see this file at all.
		const resp = await page.goto(route, { waitUntil: 'domcontentloaded' });

		// A LOGIN REDIRECT MUST NEVER READ AS COVERAGE — this is the whole of audit M3, and it made
		// every assertion in this file vacuous on an auth-enabled deploy. `page.goto` returns the
		// response of the LAST hop, so an OIDC bounce to Dex answers 200 and `toBe(200)` passes for
		// any path at all. Measured against the live k3s deploy: `/train`, `/media` and even
		// `/this-is-invented` all reported `status=200 url=…/dex/auth/local/login`. The other two
		// assertions went with it — the Dex page loads no `/_app/` asset and throws no page error, so
		// an empty `appAsset404` and an empty `pageErrors` meant "we never reached the app".
		//
		// A curl of the same paths returns an honest 404, which is why this is easy to mis-diagnose:
		// curl carries no session and gets the raw ingress answer, the browser follows the bounce.
		//
		// So: land-check first. A deploy that redirects us away is NOT covered, and says so as a SKIP
		// rather than a pass — an untested surface must not be indistinguishable from a passing one.
		const landed = new URL(page.url());
		const target = new URL(route, resp?.url() ?? page.url());
		const bouncedToAuth =
			landed.origin !== target.origin || /\/dex\/|\/oauth2\/|\/auth\/(login|callback)/.test(landed.pathname);
		test.skip(
			bouncedToAuth,
			`${route} bounced to ${page.url()} — this deploy is auth-gated and the run has no session, ` +
				`so nothing about the zone was exercised. Point RASK_E2E_BASE_URL at an auth-off install, ` +
				`or give this suite a signed-in storageState (the OIDC issuer must also be reachable from ` +
				`the browser: the redirect above targets the issuer URL, not the ingress).`,
		);

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
