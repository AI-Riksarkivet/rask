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
	media: { title: 'Search', kind: 'trigger' }, // the media read plane, named for the task
	annotator: { title: 'Annotate', kind: 'link' },
	compute: { title: 'Compute', kind: 'trigger' }, // the merged rask zone (R16)
	train: { title: 'Train', kind: 'link' }, // resurrected zone (R17), plain link while scaffolded
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
	await expect(page.getByRole('heading', { name: 'lance', exact: true })).toBeVisible();
});

test('GET /auth/logout clears the session and redirects home', async ({ page, baseURL }) => {
	const res = await page.goto('/auth/logout');
	expect(res?.status()).toBe(200);
	expect(page.url()).toBe(`${baseURL}/`);
});

test('the navbar carries one entry per zone and no governance column for an anonymous visitor', async ({
	page,
}) => {
	// Ground truth first: every zone directory EXCEPT home has exactly one row in ZONE_ENTRIES. A
	// future zone scaffolded without a navbar entry fails HERE, before any DOM assertion.
	expect(Object.keys(ZONE_ENTRIES).sort()).toEqual(zoneDirs().filter((z) => z !== 'home'));
	await page.goto('/');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	await expect(nav).toBeVisible();
	// One entry per ZONE (R15): a zone with sub-areas (Lakehouse, Search, Compute) is a
	// NavigationMenu trigger opening a panel; a single-surface zone (Home, Annotate, Train, Studio)
	// stays a plain link — a one-row dropdown would be noise.
	for (const { title, kind } of Object.values(ZONE_ENTRIES)) {
		await expect(
			nav.getByRole(kind === 'trigger' ? 'button' : 'link', { name: title, exact: true }),
		).toBeVisible();
	}
	// …and nothing beyond those entries: the bar carries the zones, the whole zones, and nothing but
	// the zones (panel rows live in portalled content, closed here, so they never count).
	const triggerCount = Object.values(ZONE_ENTRIES).filter((e) => e.kind === 'trigger').length;
	await expect(nav.getByRole('button')).toHaveCount(triggerCount);
	await expect(nav.getByRole('link')).toHaveCount(Object.keys(ZONE_ENTRIES).length - triggerCount);
	// Every zone in the bar is a DIFFERENT app, so every plain link leaves this app's route manifest
	// and must hard-navigate. There is no same-zone exception left to carve out: the one entry that
	// used to be soft was Home, and it is no longer in the bar.
	await expect(nav.getByRole('link', { name: 'Train', exact: true })).toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
	// fetchMe resolves null (no session, no catalog) → estate_admin is unknowable → fail-closed.
	// Access has no home outside the estate-admin Governance column, so it is nowhere in this bar…
	await expect(nav.getByText('Access')).toHaveCount(0);
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	// This zone SSRs the resolved triggers (fetchMe has nothing to wait for auth-off), so the buttons are
	// in the markup before the client bundle has attached bits-ui's handlers — clicking straight away
	// lands on inert HTML and the panel silently never opens.
	//
	// That wait used to be `networkidle`, and it can never fire again: every zone's shell now holds a
	// LIVE query open for the notification bell, so this app has no idle network BY DESIGN. Waiting for
	// the button to exist would not help either — it exists in the server-rendered HTML, before any
	// listener is attached. The honest condition is "clicking this actually opens the panel", so that is
	// what is asserted: click, and if nothing happened yet, click again until it does.
	await expect(async () => {
		await nav.getByRole('button', { name: 'Lakehouse', exact: true }).click();
		await expect(panel).toBeVisible({ timeout: 1000 });
	}).toPass({ timeout: 20_000 });
	// …and opening the one trigger that WOULD carry them proves the columns never rendered.
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/admin"]')).toHaveCount(0);
	// Every panel row leaves the home zone's route manifest, so it must hard-navigate. From HOME that
	// is still true of every row, including the lakehouse ones — home is its own zone.
	await expect(panel.getByRole('link', { name: /^Registry/ })).toHaveAttribute(
		'href',
		'/lakehouse/models',
	);
	await expect(panel.locator('a[href="/lakehouse/models"]')).toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
});

test('the landing shows the gallery empty state and NO auth control when auth is off', async ({
	page,
}) => {
	await page.goto('/');
	await expect(page.getByRole('heading', { name: 'lance', exact: true })).toBeVisible();
	// Auth-off + no catalog → signed out, no projects → the empty state names the unconfigured sign-in.
	await expect(page.getByText('sign-in is not configured on this stack')).toBeVisible();
	// authEnabled is false → the sign-in affordance is gated off entirely (no dead login link on an
	// ungoverned stack) — neither the gallery prompt nor a navbar menu entry.
	await expect(page.getByRole('link', { name: 'Sign out' })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Sign in' })).toHaveCount(0);
	await expect(page.getByRole('link', { name: 'Sign in' })).toHaveCount(0);
});
