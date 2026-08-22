import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from './session';
import { closePanel, openPanel } from './nav';
import { MOCK_UPSTREAMS } from './ports';

// The estate shell IN THE MODELS ZONE: the cross-zone TopNavbar, and the zone-scoped sidebar carrying only
// this zone's own routes. Moved here from `lakehouse/e2e/models/shell.spec.ts` when the routes left
// `/lakehouse/models` — and INVERTED by that move, because the thing it asserted changed sign.
//
// It used to pin the growth rule from the folded-in side: models had no top-level entry, it was a COLUMN
// of the Lakehouse panel, and the bar grew only when a ZONE did (R15). The migration promised in R17 then
// happened — the registry, experiments and pipeline physically moved out of the lakehouse — so models IS a
// zone now, and the same rule says it must carry exactly one entry of its own. Both halves are asserted
// below: this zone's trigger exists and is lit, and the lakehouse's panel no longer advertises the routes
// it gave up.
//
// The bar's entries are asserted BY NAME as well as by count: a bare `toHaveCount(6)` is what let the old
// assertions rot silently through two IA changes, and the two catch opposite defects — the names catch a
// zone silently leaving the bar, the count catches one silently joining it.
//
// NO `page.route('**/capi/v1/me')` STAND-IN, unlike the lakehouse original. `me` is resolved SERVER-SIDE
// now (`makeZoneLayoutLoad` → `fetchMe` against CATALOG_API, #107), so the browser never issues that read
// and there is nothing for page.route to intercept — the identity is SEEDED on the mock instead, exactly
// like every other server-side read in this suite. That is also what keeps the admin/anon pair testable
// here: an unseeded `/v1/me` is `fetchMe`'s honest `null`, which IS the unresolved identity.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>) =>
	page.request.post(`${MOCK_UPSTREAMS}/__mock/seed`, { data: { bearer: token, routes } });

test.beforeEach(async ({ context }, testInfo) => {
	// Auth-ON server: an anonymous navigation is redirected to /auth/login, a home-zone route absent here.
	// Per-test bearer so the fullyParallel suite shares no seeded identity.
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

/** Every zone of the estate carries exactly one entry. A MULTI-SURFACE zone renders as a panel TRIGGER
 *  (a button); a single-surface zone renders as a plain LINK, because a one-row dropdown is noise.
 *
 *  Studio is the one link, and it is not an oversight in either direction: it panelled briefly (Apps +
 *  Flows), then the mini-app launcher and the animation A/B were deleted, leaving one surface — the flow
 *  canvas at the zone root. `nav-config.ts` records the same ruling ("NO `STUDIO_ITEMS`"). This spec
 *  listed it among the triggers and asserted `links == 0`, so it was red on both counts from the day
 *  Studio un-panelled; splitting the two lists is what makes the assertion say what the bar means. */
const PANELLED = ['Lakehouse', 'Compute', 'Explorer', 'Annotate', 'Models'] as const;
const LINKED = ['Studio'] as const;

test('the zone bar carries one trigger per zone, and this zone is one of them', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN });
	await page.goto('/models');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// THE IN-PROJECT BAR — one entry per zone, and nothing else. Home, Projects and Settings are the
	// ESTATE level and are not in this bar at all (the two-level ruling). It is IDENTITY-INDEPENDENT: this
	// is the estate admin, and an admin earns panel COLUMNS, never a top-level entry.
	for (const zone of PANELLED) {
		await expect(nav.getByRole('button', { name: zone, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('button')).toHaveCount(PANELLED.length);
	// The single-surface zones are LINKS, and with no panel open they are the only links in the bar — so
	// this count is what catches a zone that lost its panel and started navigating on click instead.
	for (const zone of LINKED) {
		await expect(nav.getByRole('link', { name: zone, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('link')).toHaveCount(LINKED.length);
	for (const estate of ['Home', 'Projects', 'Settings']) {
		await expect(nav.getByRole('link', { name: estate, exact: true })).toHaveCount(0);
		await expect(nav.getByRole('button', { name: estate, exact: true })).toHaveCount(0);
	}
	// Access is a ROW of the main menu's Settings panel, which does not render inside a zone — never a
	// top-level entry here, in any shape.
	await expect(nav.getByText('Access')).toHaveCount(0);
	// THIS zone's trigger is the lit one: `under('/models')` against the zone root.
	await expect(nav.getByRole('button', { name: 'Models', exact: true })).toHaveAttribute(
		'data-active',
		'',
	);
});

test('this zone owns its panel, and its rows link where they claim', async ({ page }) => {
	// The growth rule the IA rests on: a new ROUTE becomes a row in a panel, only a new ZONE earns an
	// entry. Models earned one — so assert its rows actually resolve to this zone rather than merely being
	// labelled for it.
	await page.goto('/models');
	const panel = await openPanel(page, 'Models');
	for (const [row, href] of [
		['Registry', '/models/'],
		['Experiments', '/models/experiments'],
		['Training runs', '/models/runs'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
	// EXACTLY these rows — asserted as a count, not just per-row, because this spec's per-row loop is
	// what let two dead rows sit here: `Pipeline` (route deleted 2026-08-07) and `Playground`
	// (`/models/playground`, a 404 from the day #131 moved inference to `/compute/inference`). Both
	// satisfied "the row's href is what the config says" while pointing at nothing.
	// Scoped to this zone's own hrefs rather than every link in the viewport: the count must pin the ROWS,
	// and it must not go red the day the panel grows chrome that happens to be an anchor.
	await expect(panel.locator('a[href^="/models"]')).toHaveCount(3);
	// And the two retired addresses specifically, so a resurrected row fails HERE rather than at runtime.
	await expect(panel.locator('a[href^="/models/pipeline"]')).toHaveCount(0);
	await expect(panel.locator('a[href^="/models/playground"]')).toHaveCount(0);
	// Registry IS this zone's root, so the panel skips its synthesized "open the zone" row and the root is
	// reachable exactly once — no duplicate.
	await expect(panel.locator('a[href="/models/"]')).toHaveCount(1);
});

test('the lakehouse panel no longer advertises the routes this zone took', async ({ page }) => {
	await page.goto('/models');
	const panel = await openPanel(page, 'Lakehouse');
	// The lakehouse keeps the catalog and model LINEAGE (which run wrote which version — a lineage
	// question); what it must not keep is a Models column pointing at routes it no longer serves. Both
	// addresses are checked: the old one because a resurrected row must fail here, and the new one because
	// a Lakehouse panel that grew a `/models/*` row would be the same defect at a new address.
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Lineage', { exact: true })).toBeVisible();
	await expect(panel.getByText('Models', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/models"]')).toHaveCount(0);
	await expect(panel.locator('a[href^="/models"]')).toHaveCount(0);
});

test('an estate admin earns the Operations COLUMN, not a top-level entry', async ({ page }) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN });
	await page.goto('/models');
	// Privilege shows up only INSIDE a panel. Operations stays with the lakehouse — running the estate is
	// an operation ON it — so the split is asserted from both sides: these rows are in that zone's panel
	// and the governance ones are not (they moved to the home zone's /settings, which is estate level and
	// therefore not in this bar at all).
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Operations', { exact: true })).toBeVisible();
	for (const row of ['/lakehouse/admin/streams', '/lakehouse/admin/dlq']) {
		await expect(panel.locator(`a[href="${row}"]`)).toBeVisible();
	}
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	// Both addresses: the old one because a resurrected row must fail here, and the new one because a
	// Lakehouse panel that grew a `/settings/*` row would be the same defect at a new address.
	await expect(panel.locator('a[href="/lakehouse/governance/access"]')).toHaveCount(0);
	await expect(panel.locator('a[href="/settings/access"]')).toHaveCount(0);
	// …and with the panel closed the bar itself still carries no Access entry of any kind.
	await closePanel(page);
	await expect(page.getByRole('navigation', { name: 'Zones' }).getByText('Access')).toHaveCount(0);
});

test('an unresolved identity gets no Operations column (fail-closed)', async ({ page }) => {
	// NOTHING seeded, so the catalog 404s this bearer's `/v1/me` and `fetchMe` degrades to null — the
	// unresolved identity. The bar must look IDENTICAL (same triggers, same count: privilege is never a
	// top-level entry), and the privileged column must simply be absent.
	await page.goto('/models');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	for (const zone of PANELLED) {
		await expect(nav.getByRole('button', { name: zone, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('button')).toHaveCount(PANELLED.length);
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/admin"]')).toHaveCount(0);
});

test('the sidebar renders ONLY this zone routes, and marks the zone root active', async ({
	page,
}) => {
	await page.goto('/models');
	// Registry is the zone ROOT here — matched with `exact`, so it is lit on `/models` and would not be on
	// a sibling area. Training moved off the root onto `/models/submit` for exactly that reason.
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Registry' })).toBeVisible();
	const sidebar = page.locator('[data-sidebar="content"]');
	// The REAL leaves of `MODELS_ZONE_NAV`. This list carried two that the rail never rendered:
	// `Playground` was never a leaf of this zone at all (inference is `/compute/inference`), and
	// `Pipeline` was one until its route was deleted 2026-08-07. Asserting a leaf that does not exist
	// fails loudly, which is the good case — the same two names sat in the NAVBAR row list, where the
	// assertion only checked each row's href attribute and so stayed green over a 404.
	for (const leaf of ['Experiments', 'Submit', 'Runs', 'Monitoring', 'Analysis']) {
		await expect(sidebar.getByRole('link', { name: leaf, exact: true })).toBeVisible();
	}
	await expect(sidebar.getByRole('link', { name: 'Pipeline', exact: true })).toHaveCount(0);
	await expect(sidebar.getByRole('link', { name: 'Playground', exact: true })).toHaveCount(0);
	// The rail is ZONE-scoped: no other zone's areas leak into it.
	await expect(sidebar.locator('a[href^="/lakehouse"]')).toHaveCount(0);
	await expect(sidebar.locator('a[href^="/compute"]')).toHaveCount(0);
});

test('the project switcher IS the sidebar header — exactly one control, in the project-context slot', async ({
	page,
}) => {
	// The sidebar header IS the project dropdown (which project you are in, the projects you can move to,
	// and the way back to the main menu, in one control). The rail's most valuable slot must not spend
	// itself printing the ZONE name, which the navbar highlight and the breadcrumb already say twice.
	await seed(page, { 'GET /v1/me': ME_ADMIN });
	await page.goto('/models');
	const switcher = page.getByRole('button', { name: 'Switch project' });
	await expect(switcher).toBeVisible();
	// It lives in the sidebar header…
	const header = page.locator('[data-sidebar="header"]');
	await expect(header).toHaveCount(1);
	await expect(header.getByRole('button', { name: 'Switch project' })).toBeVisible();
	// …and ONLY there. Two switchers is the regression this pins: a navbar copy would satisfy every other
	// check in this file.
	await expect(page.getByRole('button', { name: 'Switch project' })).toHaveCount(1);
	// Geometry: it heads the rail — above the zone routes, and the rail is left of the navbar's zone
	// links, so the switcher still reads as global context rather than as one more in-zone route.
	const switcherBox = (await switcher.boundingBox())!;
	const zonesBox = (await page.getByRole('navigation', { name: 'Zones' }).boundingBox())!;
	const navBox = (await page.locator('[data-sidebar="content"]').boundingBox())!;
	expect(switcherBox.x).toBeLessThan(zonesBox.x);
	expect(switcherBox.y).toBeLessThan(navBox.y);
});
