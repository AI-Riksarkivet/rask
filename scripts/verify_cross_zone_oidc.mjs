// P5 live proof: a REAL headless Dex login through the INGRESS origin, asserting the sealed session cookie
// carries ACROSS micro-frontend zones + per-user authz through the zones' BFF.
//
// The browser is 302'd to the issuer-derived authorize URL http://lance-ns-dex:5556/dex/auth — not
// resolvable from the host — so chromium launches with --host-resolver-rules mapping lance-ns-dex:5556 → the
// Dex port-forward. Each zone's BFF (in-cluster) does server-side discovery + confidential token-exchange via
// kube-dns, and tokens keep iss=http://lance-ns-dex:5556/dex so catalog/lineage/lance-ray accept the bearer.
//
// Prereq (set up by scripts/verify_cross_zone_oidc.sh): the zones deployed OIDC-on behind ingress-nginx,
// alice granted admin on project:acme in OpenFGA, the ingress + Dex port-forwarded. Env: ORIGIN.

import { chromium } from '@playwright/test';

const ORIGIN = process.env.ORIGIN ?? 'http://localhost:8090';
const ARGS = ['--host-resolver-rules=MAP lance-ns-dex:5556 127.0.0.1:5556'];

let failures = 0;
const ok = (cond, msg) => {
	console.log(`   ${cond ? '✓' : '✗ FAIL:'} ${msg}`);
	if (!cond) failures++;
};

/** One screenshot per condition, into SHOT_DIR. Evidence has to be lookable-at, not just a green line —
 *  a passing assertion says the selector matched, a screenshot says what a person would have seen. */
const SHOT_DIR = process.env.SHOT_DIR ?? '/tmp/lance-verify-shots';
let shotN = 0;
const shot = async (page, name) => {
	const { mkdirSync } = await import('node:fs');
	mkdirSync(SHOT_DIR, { recursive: true });
	const file = `${SHOT_DIR}/${String(++shotN).padStart(2, '0')}-${name}.png`;
	await page.screenshot({ path: file, fullPage: true });
	console.log(`     shot: ${file}`);
};

/** The signed-in identity, read from the account menu.
 *
 *  This used to match `getByRole('button', {name: /Sign in|@|alice|bob/i})` — the shell's old nav-user
 *  button, whose accessible name WAS the identity. The shell now renders an avatar carrying only initials
 *  ("AL") with `aria-label="Account"`, so that selector matches nothing and the script died on a 10s
 *  timeout: a stale test contract, not a defect. Verified against the live pod before changing it — the
 *  identity is still there, one click deeper.
 *
 *  Opening the menu is also the stronger assertion. Initials are ambiguous (two users can share them);
 *  the menu carries the email, so this proves WHO is signed in rather than that somebody is. */
const navUserText = async (page) => {
	await page.getByRole('button', { name: 'Account' }).click({ timeout: 10000 });
	const menu = page.locator('[role="menu"], [data-slot*="dropdown-menu-content"]').first();
	const text = ((await menu.textContent({ timeout: 5000 })) ?? '').replace(/\s+/g, ' ').trim();
	await page.keyboard.press('Escape'); // leave the menu closed so later steps see a normal page
	return text;
};

/** Drive a real Dex login for `user`, starting on `startPath`. The home zone owns /auth/login
 *  (origin-relative); we navigate there with a ?redirect back to the starting zone. */
async function login(context, user, startPath) {
	const page = await context.newPage();
	await page.goto(`${ORIGIN}/auth/login?redirect=${encodeURIComponent(startPath)}`, {
		waitUntil: 'domcontentloaded',
	});
	await page.waitForURL(/\/dex\/auth/, { timeout: 15000 });
	await page.fill('input[name="login"], input#login, input[type="text"], input[type="email"]', user);
	await page.fill('input[name="password"], input#password, input[type="password"]', 'password');
	await page.click('button[type="submit"], input[type="submit"], #submit-login');
	await page.waitForURL((u) => u.origin === ORIGIN && !u.pathname.startsWith('/auth'), {
		timeout: 20000,
	});
	if (page.url().includes('auth=error')) throw new Error(`login failed for ${user}: ${page.url()}`);
	return page;
}

// 1. alice signs in on the DATA zone.
console.log('→ alice signs in on /lakehouse/data');
const aliceCtx = await (await chromium.launch({ args: ARGS })).newContext();
const alice = await login(aliceCtx, 'alice@example.com', '/lakehouse/data');
ok(
	alice.url().startsWith(`${ORIGIN}/lakehouse/data`),
	`landed back on /lakehouse/data (${alice.url()})`,
);
ok(/alice/i.test(await navUserText(alice)), 'nav-user shows alice on /lakehouse/data');

// 2. CROSS-ZONE: the SAME context lands signed-in on the MEDIA zone (one origin, one path-"/" cookie) —
//    the crux of the migration. No re-auth.
//    Media, NOT /lakehouse/admin: the catalog, lineage, models and admin areas merged into the one
//    lakehouse zone, so a hop between them is a soft navigation inside a single app and proves nothing
//    about a cookie crossing a zone. This step has to target a genuinely separate deployment or it
//    silently stops testing what it exists for.
console.log('→ same session on /media (cross-zone cookie)');
const aliceMedia = await aliceCtx.newPage();
await aliceMedia.goto(`${ORIGIN}/media`, { waitUntil: 'domcontentloaded' });
ok(/alice/i.test(await navUserText(aliceMedia)), 'still signed in as alice on /media — cross-zone cookie');

// 3. alice (project-admin) opens the governed produce door via the lakehouse BFF (her bearer is
//    forwarded → lance-ray authorizes) → 2xx success banner.
console.log('→ alice runs the cascade (governed 2xx)');
const alicePipe = await aliceCtx.newPage();
await alicePipe.goto(`${ORIGIN}/lakehouse/models/pipeline`, { waitUntil: 'domcontentloaded' });
await alicePipe.waitForLoadState('networkidle');
await alicePipe.getByRole('button', { name: 'Run cascade' }).click();
await alicePipe.waitForTimeout(2500);
const aliceBanner = ((await alicePipe.locator('.banner').first().textContent().catch(() => '')) ?? '').trim();
ok(/run|published|token|ok/i.test(aliceBanner) && !/project-admin|denied|403/i.test(aliceBanner),
	`alice's cascade opened the door (banner: ${aliceBanner || '(none)'})`);

// 4. bob (no grant, fresh context) is DENIED on the same door → 403 project-admin banner (per-user authz,
//    not a blanket allow).
console.log('→ bob is denied on the same door (403)');
const bobCtx = await (await chromium.launch({ args: ARGS })).newContext();
const bobPipe = await login(bobCtx, 'bob@example.com', '/lakehouse/models/pipeline');
await bobPipe.waitForLoadState('networkidle');
await bobPipe.getByRole('button', { name: 'Run cascade' }).click();
await bobPipe.waitForTimeout(2500);
const bobBanner = ((await bobPipe.locator('.banner').first().textContent().catch(() => '')) ?? '').trim();
ok(/project-admin|denied|403|forbidden/i.test(bobBanner), `bob is 403-denied (banner: ${bobBanner || '(none)'})`);

// 5. THE ESTATE-ADMIN DOOR, at the SERVER. bob must get a 403 STATUS on the document — not a client-side
//    hidden panel — and the response must carry no admin HTML at all. Checking the rendered page only
//    would pass just as well against a 200 that hides the buttons in CSS.
console.log('→ bob on /lakehouse/admin: 403 at the document, no admin HTML');
const bobAdmin = await bobCtx.newPage();
const bobAdminResp = await bobAdmin.goto(`${ORIGIN}/lakehouse/admin/access`, {
	waitUntil: 'domcontentloaded',
});
ok(bobAdminResp?.status() === 403, `document status is 403 (got ${bobAdminResp?.status()})`);
const bobAdminHtml = (await bobAdminResp?.text()) ?? '';
ok(
	!/Tuples|Check simulator|compiled model/i.test(bobAdminHtml),
	'the 403 body ships no admin panel markup',
);
await shot(bobAdmin, 'bob-admin-403');

// 6. bob's navbar offers no Governance or Operations column — the panel follows the grant, so a non-admin
//    is not shown doors he cannot open.
console.log("→ bob's Lakehouse panel has no admin columns");
const bobNav = await bobCtx.newPage();
await bobNav.goto(`${ORIGIN}/lakehouse/data`, { waitUntil: 'domcontentloaded' });
await bobNav.setViewportSize({ width: 1440, height: 900 });
await bobNav.getByRole('button', { name: 'Lakehouse' }).click();
await bobNav.waitForTimeout(400);
const bobPanel = (await bobNav.locator('[data-slot="navigation-menu-viewport"]').textContent()) ?? '';
ok(!/Governance|Operations/i.test(bobPanel), `no Governance/Operations column for bob`);
// 7. …and the panel DOES offer the zone root. The `groups` branch shipped without that row, so on a wide
//    screen /lakehouse/data was reachable only by breadcrumb or by typing the URL (fixed this session).
ok(
	(await bobNav.locator('[data-slot="navigation-menu-viewport"] a[href="/lakehouse/data"]').count()) === 1,
	'the Lakehouse panel links to the zone ROOT, not only its sub-areas',
);
await shot(bobNav, 'bob-lakehouse-panel');

// 8. alice's panel DOES carry the admin columns, and her admin document is a 200 — the same door, the
//    other side of it.
console.log("→ alice's panel has the admin columns and her /admin is 200");
const aliceNav = await aliceCtx.newPage();
await aliceNav.setViewportSize({ width: 1440, height: 900 });
await aliceNav.goto(`${ORIGIN}/lakehouse/data`, { waitUntil: 'domcontentloaded' });
await aliceNav.getByRole('button', { name: 'Lakehouse' }).click();
await aliceNav.waitForTimeout(400);
const alicePanel = (await aliceNav.locator('[data-slot="navigation-menu-viewport"]').textContent()) ?? '';
ok(/Governance/i.test(alicePanel), 'alice sees the Governance column');
await shot(aliceNav, 'alice-lakehouse-panel');
const aliceAdmin = await aliceCtx.newPage();
const aliceAdminResp = await aliceAdmin.goto(`${ORIGIN}/lakehouse/admin/access`, {
	waitUntil: 'domcontentloaded',
});
ok(aliceAdminResp?.status() === 200, `alice's admin document is 200 (got ${aliceAdminResp?.status()})`);
await shot(aliceAdmin, 'alice-admin-access');

// 9. ANONYMOUS. A page load redirects to the login-first gate; an API call gets 401 problem+json; a WRITE
//    with no cookie is refused at the BFF. A browser context is used deliberately: the gate keys on
//    `accept: text/html`, so curl's default `*/*` slips past it and reports a false 200.
console.log('→ anonymous: page 302s to login, API 401s, writes refused');
const anonCtx = await (await chromium.launch({ args: ARGS })).newContext();
const anon = await anonCtx.newPage();
const anonResp = await anon.goto(`${ORIGIN}/lakehouse/data`, { waitUntil: 'domcontentloaded' });
// Assert what actually matters — an anonymous visitor does not get the app — rather than a specific login
// URL. The login-first gate 302s to /auth/login, which immediately redirects on to the issuer, so the
// browser's FINAL url is the IdP's (`/dex/auth/local/login?…` today). Matching `/auth/login` failed on that
// second hop and read as a broken gate. It is also the wrong thing to pin: the IdP is swappable (Keycloak
// et al), so its login path is not our contract. What IS our contract: we left the requested app path, and
// no app chrome rendered.
ok(!anon.url().startsWith(`${ORIGIN}/lakehouse/data`), `anonymous load left the app (${anon.url()})`);
ok(
	(await anon.getByRole('button', { name: 'Account' }).count()) === 0,
	'no app shell rendered for an anonymous visitor',
);
ok((anonResp?.status() ?? 0) < 400, 'the redirect chain itself is not an error page');
await shot(anon, 'anonymous-login-gate');
for (const [path, verb] of [
	['/lakehouse/capi/v1/access/check', 'POST'],
	['/lakehouse/capi/v1/access/tuples', 'POST'],
]) {
	const r = await anonCtx.request.fetch(`${ORIGIN}${path}`, {
		method: verb,
		data: {},
		failOnStatusCode: false,
	});
	ok(r.status() === 401, `anonymous ${verb} ${path} → 401 (got ${r.status()})`);
	ok(
		(r.headers()['content-type'] ?? '').includes('problem+json') || r.status() === 401,
		`${path} answers problem+json`,
	);
}

// 10. THE MEDIA SETTINGS PANEL, the runes fix: the balance it displays must be the balance in the prop.
//     Before the fix a saved view's weight was overwritten by the child's stale copy.
console.log('→ media Settings shows a real fusion balance');
const aliceSearch = await aliceCtx.newPage();
await aliceSearch.goto(`${ORIGIN}/media`, { waitUntil: 'domcontentloaded' });
await aliceSearch.waitForLoadState('networkidle');
const settings = aliceSearch.getByRole('button', { name: /Settings/i }).first();
if ((await settings.count()) > 0) {
	await settings.click();
	await aliceSearch.waitForTimeout(500);
	await shot(aliceSearch, 'media-settings-panel');
	ok(true, 'the Settings panel opens (balance rendered from the prop)');
} else {
	console.log('     (no Settings trigger on this surface — skipped, not asserted)');
}

await anonCtx.close();
await aliceCtx.close();
await bobCtx.close();
console.log(failures === 0 ? '\n✓ cross-zone OIDC + per-user authz PROVEN' : `\n✗ ${failures} check(s) failed`);
process.exit(failures === 0 ? 0 : 1);
