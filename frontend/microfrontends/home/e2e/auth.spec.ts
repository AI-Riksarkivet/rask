import { test, expect } from '@playwright/test';
// The zone-directory ground truth (a dir under microfrontends/ with a package.json), read straight
// off the filesystem by the shared zone-contract helper. Relative import on purpose: Playwright
// transpiles local files but not node_modules, and importing the ui package's nav-config here would
// make the test self-referential — it would pass whatever the config said, missing zone and all.
import { zoneDirs } from '../../../packages/zone-contract/src/manifest';

// One navbar entry per zone (R15) — keyed by ZONE DIRECTORY, so the map's keys are asserted equal to
// `zoneDirs()`: scaffolding a new zone without adding its navbar entry (and its row here) FAILS this
// suite instead of silently shipping an unreachable zone. `trigger` = a NavigationMenu button opening
// a panel of sub-areas; `link` = a plain link (single-surface zone).
//
// `home` is deliberately ABSENT and is asserted separately below. It is the catch-all zone reached by
// the shell's brand header ("Back to home", app-sidebar.svelte) from every other zone — the ordinary
// web convention — so a navbar row for it would, on this very page, be a link to the page you are
// already standing on. Dropping it is what `zoneDirs().filter(z => z !== 'home')` encodes; the guard
// against an UNREACHABLE zone is not weakened, only moved: home's reachability is proved by the brand
// assertion, and every other zone still fails here if it ships without an entry.
const ZONE_ENTRIES: Record<string, { title: string; kind: 'trigger' | 'link' }> = {
	lakehouse: { title: 'Lakehouse', kind: 'trigger' },
	explorer: { title: 'Explorer', kind: 'trigger' }, // the corpus read plane; 'Search' died with the `media` directory
	annotator: { title: 'Annotate', kind: 'link' },
	compute: { title: 'Compute', kind: 'trigger' }, // the merged rask zone (R16)
	models: { title: 'Models', kind: 'trigger' }, // REPLACED `train` (R17 → #108), on train's own port
	studio: { title: 'Studio', kind: 'link' }, // the sandbox/PoC zone (R17)
};

// The home zone owns the relocated OIDC BFF routes (/auth/{login,callback,logout}) AND the
// signed-in landing (navbar + project gallery). Running auth-OFF (no OIDC env, no catalog), the
// routes must exist and FAIL SAFE — login/logout are no-op redirects home, never 404s or 500s —
// and the landing must show no auth affordance, the base navbar (no estate-admin entries: fetchMe
// resolves null), and the gallery's empty state. The real per-user round-trip (login → Dex →
// callback → sealed cookie → cross-zone session) is the live P5 drive.

test('GET /auth/login is a fail-safe no-op redirect home when auth is unconfigured', async ({
	page,
	baseURL,
}) => {
	// The route EXISTS in the home zone (not a 404) and, auth-off, redirects home rather than erroring or
	// starting a flow against a missing IdP. Following the 302 lands on the home landing.
	const res = await page.goto('/auth/login');
	expect(res?.status()).toBe(200); // followed the 302 → the landing rendered (not a 404/500)
	expect(page.url()).toBe(`${baseURL}/`);
	// The h1 is the LAGOM wordmark now (#144): two flex spans around the peepo image, so the
	// accessible name computes with a separator space — match loosely, never `exact`.
	await expect(page.getByRole('heading', { name: /LA\s*GOM/ })).toBeVisible();
});

test('GET /auth/logout clears the session and redirects home', async ({ page, baseURL }) => {
	const res = await page.goto('/auth/logout');
	expect(res?.status()).toBe(200);
	expect(page.url()).toBe(`${baseURL}/`);
});

test('the MAIN MENU navbar carries Home and Projects for an anonymous visitor — no zone, no Settings', async ({
	page,
}) => {
	// REWRITTEN BY RULING (2026-08-03): "we should only see 2 items in topnavbar — projects and
	// settings, nothing else". This test used to assert the OPPOSITE here — every zone of the estate
	// present in the bar at `/` — which was right while one bar served every route. It is not the
	// estate root's bar any more: standing at the main menu you are choosing what to work on, not
	// moving between zones, so the shell swaps in `mainMenuNav()` on `zoneOf(pathname) === ''`.
	//
	// The R15 contract that used to live here — every zone directory has exactly one navbar entry —
	// did NOT disappear with it. It moved to where it can still be checked: `topNav()` is asserted
	// zone-by-zone in `@rask/ui`'s `tests/nav-config.test.ts`, and the rendered zone bar is asserted
	// in the lakehouse and explorer suites, which run inside a zone. ZONE_ENTRIES stays the ground-truth
	// table for it, and is still diffed against the zone DIRECTORIES right here, before any DOM
	// assertion — so a zone scaffolded without a navbar entry still fails in this file.
	expect(Object.keys(ZONE_ENTRIES).sort()).toEqual(zoneDirs().filter((z) => z !== 'home'));
	await page.goto('/');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	await expect(nav).toBeVisible();
	// THE main-menu contract: Home and Projects, both plain links to this zone's own routes. Home is
	// the estate landing — one of the three places you can be at this level, not a "back" affordance,
	// which is why it is nameable here and absent from the in-project bar.
	await expect(nav.getByRole('link', { name: 'Home', exact: true })).toHaveAttribute('href', '/');
	await expect(nav.getByRole('link', { name: 'Projects', exact: true })).toHaveAttribute(
		'href',
		'/projects',
	);
	// …and NOT ONE zone entry beside it, in either role. Asserted per zone by name rather than as a
	// bare count: a count says "one thing changed", a name says which, and this is precisely the
	// assertion that would otherwise rot the next time a zone is added.
	for (const { title } of Object.values(ZONE_ENTRIES)) {
		await expect(nav.getByRole('button', { name: title, exact: true })).toHaveCount(0);
		await expect(nav.getByRole('link', { name: title, exact: true })).toHaveCount(0);
	}
	await expect(nav.getByRole('button')).toHaveCount(0);
	await expect(nav.getByRole('link')).toHaveCount(2);
	// SETTINGS is the third main-menu entry and it is estate-admin ONLY. fetchMe resolves null here
	// (no session, no catalog) → estate_admin unknowable → fail-closed, so an anonymous visitor's main
	// menu is two entries, and the bar never names a surface this viewer is barred from.
	await expect(nav.getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
	// Access lives in the Settings panel now; nowhere in an anonymous bar, panel open or closed.
	await expect(nav.getByText('Access')).toHaveCount(0);
	// There is NO panel to open at all here — the main menu's one entry is a plain link. The rows that
	// used to be asserted from this page (the Lakehouse panel's Registry row, its hard-navigation, the
	// absence of /lakehouse/admin rows for a non-admin) are panel behaviour, and panels now only exist
	// inside a zone: the lakehouse suite asserts them from a lakehouse page, which is the only place a
	// user can reach them. Pinned here as the absence, so a zone panel reappearing at the estate root
	// fails rather than passing quietly.
	await expect(page.locator('[data-slot="navigation-menu-viewport"]')).toHaveCount(0);
	await expect(nav.locator('a[href^="/lakehouse"]')).toHaveCount(0);
});

test('the landing is HOME — an honest scaffold, not the project gallery', async ({ page }) => {
	await page.goto('/');
	// The h1 is the LAGOM wordmark now (#144): two flex spans around the peepo image, so the
	// accessible name computes with a separator space — match loosely, never `exact`.
	await expect(page.getByRole('heading', { name: /LA\s*GOM/ })).toBeVisible();
	// IT SAYS WHAT IT IS. The insights are not wired, and a landing page that implied otherwise — or
	// showed invented numbers — would be worse than one that admits it. The badge is the claim.
	await expect(page.getByText('Scaffold — no insights wired yet')).toBeVisible();
	await expect(page.getByRole('region', { name: 'Estate insights' })).toBeVisible();
	// …and it is NOT the gallery any more. That surface, and the membership empty state that used to
	// greet a project-less visitor as their entire product, live on /projects — asserted as an
	// ABSENCE here so the two cannot quietly both render the list again.
	await expect(page.getByText('You are not a member of any project yet')).toHaveCount(0);
	await expect(page.getByText('sign-in is not configured on this stack')).toHaveCount(0);
	// Navigation lives in the TOPNAVBAR alone (2026-08-05): the Projects/Settings cards this page
	// used to render duplicated it — asserted as an ABSENCE so they cannot quietly return.
	await expect(page.locator('[data-slot="home-projects-card"]')).toHaveCount(0);
	await expect(page.locator('[data-slot="home-settings-card"]')).toHaveCount(0);
	// authEnabled is false → the sign-in affordance is gated off entirely (no dead login link on an
	// ungoverned stack) — neither a page prompt nor a navbar menu entry.
	await expect(page.getByRole('link', { name: 'Sign out' })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'Sign in' })).toHaveCount(0);
});
