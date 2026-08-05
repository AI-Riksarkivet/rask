import { test, expect, type Page, type Route } from '@playwright/test';

// The estate shell in the LINEAGE zone: the cross-zone TopNavbar fed by a mocked /v1/me (hermetic —
// the layout fetches it through this zone's /capi/v1/me pass-through), the zone-scoped sidebar
// carrying exactly this zone's views (Datasets / Jobs / Runs / Columns + the Graph at the root), and
// the two-row header — navbar row above, breadcrumb bar below.
//
// The bar's IA is ONE ENTRY PER ZONE (R15), grouped by domain — never a flat list of routes. A zone
// that owns sub-areas renders as a NavigationMenu TRIGGER (a button opening a panel): Lakehouse
// (Catalog + Models + Lineage, plus Governance + Operations for an estate admin), Compute, Search.
// A single-surface zone stays a plain LINK: Annotate, Train, Studio. Home is the product
// mark, not an entry. Both sets are asserted BY NAME below — a bare `toHaveCount(2)` is what let
// these assertions rot silently through the seven-zone IA (db15ea8, which gave Compute its panel)
// and the workbench zone (117c8ed, the 8th zone).

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/** Open a navbar panel and hand back its viewport.
 *
 * The triggers are server-rendered, so on a loaded machine a click can land before bits-ui has
 * attached its handlers: the markup is inert rather than broken and the panel silently never
 * opens. Retrying the click rides out that race — what must hold (the panel DOES open, and carries
 * the rows asserted by the caller) is unchanged; only the delivery is made robust. It clicks only
 * while the panel is closed, so a retry can never toggle an already-open panel back shut. */
const openPanel = async (page: Page, name: string) => {
	const trigger = page
		.getByRole('navigation', { name: 'Zones' })
		.getByRole('button', { name, exact: true });
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(async () => {
		if (!(await panel.isVisible())) await trigger.click();
		await expect(panel).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 20_000 });
	return panel;
};

test.beforeEach(async ({ page }) => {
	// The explorer's own backend calls — empty world is fine for shell assertions.
	await page.route('**/lakehouse/api/**', (route) => json(route, {}));
	await page.route('**/capi/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (path.endsWith('/v1/me')) return route.fallback();
		return json(route, {});
	});
});

test('an estate admin gets the zone triggers + the Marquez-parity sidebar leaves', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) =>
		json(route, {
			sub: 'user:alice',
			name: 'Alice',
			email: 'alice@example.com',
			estate_admin: true,
			projects: [{ project: 'acme', role: 'admin' }],
		}),
	);
	await page.goto('/lakehouse/lineage');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// THIS IS THE IN-PROJECT BAR — one entry per ZONE and nothing else. The two-level ruling took
	// Home, Projects and Settings out of it: they are the ESTATE level, and you meet them at `/`,
	// `/projects` and `/settings`, where the shell swaps in the main-menu bar instead. Inside a
	// project the bar's only job is moving between zones, and it is identity-independent again —
	// an admin earns panel COLUMNS here, never a new destination.
	//
	// Named AND counted: the name list catches a zone silently leaving the bar (the R15 defect), the
	// count catches one silently joining it.
	for (const trigger of ['Lakehouse', 'Compute', 'Search']) {
		await expect(nav.getByRole('button', { name: trigger, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('button')).toHaveCount(3);
	// The bar's LINKS are the single-surface zones — one surface means a one-row dropdown would be
	// noise; each panel TRIGGER must stay a button, or clicking it would navigate instead of opening
	// the panel.
	for (const link of ['Annotate', 'Train', 'Studio']) {
		await expect(nav.getByRole('link', { name: link, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('link')).toHaveCount(3);
	// The three estate-level entries are ABSENT here, in either role — pinned, because their leaving
	// this bar IS the ruling and a regression would otherwise just look like a longer bar.
	for (const estate of ['Home', 'Projects', 'Settings']) {
		await expect(nav.getByRole('link', { name: estate, exact: true })).toHaveCount(0);
		await expect(nav.getByRole('button', { name: estate, exact: true })).toHaveCount(0);
	}
	// THE TIER GAP: Lakehouse and Compute lead, then a spacer, then the task destinations. Rendered
	// from the `tier` data rather than a hardcoded index, so re-tiering an entry moves it.
	await expect(nav.locator('[data-slot="navbar-tier-gap"]')).toHaveCount(1);
	// The zone sidebar lists exactly the four first-class views + the Graph (active at the root).
	// Scoped to the sidebar: page content may legitimately link to the same views (e.g. the graph
	// header's capped hint links to Datasets), which would trip strict mode on a page-wide query.
	const sidebar = page.locator('[data-sidebar="content"]');
	for (const leaf of ['Datasets', 'Jobs', 'Runs', 'Columns', 'Graph']) {
		await expect(sidebar.getByRole('link', { name: leaf, exact: true })).toBeVisible();
	}
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Graph' })).toBeVisible();
});

test('the Datasets leaf lights on its list page and stays lit on a nested detail page', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/lineage/datasets');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Datasets' })).toBeVisible();
	await page.goto('/lakehouse/lineage/datasets/silver%24features');
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Datasets' })).toBeVisible();
});

test('a signed-out / unresolved identity gets NO governance column (fail-closed)', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/lineage');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// The bar never advertises privilege — the same triggers whoever is looking…
	for (const trigger of ['Lakehouse', 'Compute', 'Search']) {
		await expect(nav.getByRole('button', { name: trigger, exact: true })).toBeVisible();
	}
	// …and the same NUMBER of them: a privileged surface must never earn a top-level entry. Named
	// above and counted here (see the file header on why a bare count alone is not enough).
	await expect(nav.getByRole('button')).toHaveCount(3);
	// …so the fail-closed guarantee now lives one level in, on Lakehouse's COLUMNS. This is what
	// the old "no Admin entry in the bar" assertion stood for: with the admin surfaces folded into
	// a shared trigger, hiding them means the Governance/Operations columns never render at all and
	// not one /admin row is reachable for an unresolved identity.
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Models', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/admin"]')).toHaveCount(0);
});

test('an estate admin gets Operations in the Lakehouse panel and Governance under Settings', async ({
	page,
}) => {
	// The positive half of the guarantee above, RE-SPLIT by the 2026-08-03 ruling ("governance and
	// other setting stuff, more in terms of auth, under settings"). Operating THIS estate — events,
	// streams, dead letters — is an operation on the lakehouse and stays a column of its panel. Who
	// may do what — access, tenants, audit — is estate-wide configuration and moved to its own
	// Settings entry. Both halves are asserted here, in the same test, because the thing worth pinning
	// is the SPLIT: each surface in exactly one panel, and never in both.
	await page.route('**/capi/v1/me', (route) =>
		json(route, {
			sub: 'user:alice',
			name: 'Alice',
			email: 'alice@example.com',
			estate_admin: true,
			projects: [{ project: 'acme', role: 'admin' }],
		}),
	);
	await page.goto('/lakehouse/lineage');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	const panel = await openPanel(page, 'Lakehouse');
	for (const label of ['Catalog', 'Models', 'Operations']) {
		await expect(panel.getByText(label, { exact: true })).toBeVisible();
	}
	// Governance is NOT a column here any more — asserted as an absence so the rows cannot come to
	// exist in two places, which is the whole point of moving them.
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	for (const [row, href] of [
		['Events', '/lakehouse/admin/events'],
		['Streams', '/lakehouse/admin/streams'],
		['DLQ', '/lakehouse/admin/dlq'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
	// The governance half is NOT assertable from here any more, and that is the point: Settings is an
	// ESTATE-level entry, so it is not in this bar at all. Its rows are pinned where a user can
	// actually reach them — `home/e2e/projects/settings.spec.ts`, which drives `/settings` itself and
	// checks each row's href AND its data-sveltekit-reload (those pages are still served by THIS zone,
	// so the link crosses zones). What this test still owns is the absence:
	await expect(nav.getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Settings', exact: true })).toHaveCount(0);
	// …and with the panel closed the bar itself still carries no Access entry of any shape.
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	await expect(nav.getByText('Access')).toHaveCount(0);
});

test('a domain trigger opens a panel of its rows — pointer and keyboard, same-zone rows soft-navigating', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/lineage');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	const lakehouse = nav.getByRole('button', { name: 'Lakehouse', exact: true });
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	// Closed by default: the panel's rows are not in the document at all (nothing to tab into).
	await expect(panel).toHaveCount(0);

	// Pointer half: a click on the trigger opens it (retried only against the pre-hydration race).
	await openPanel(page, 'Lakehouse');
	// The rows link where they claim — the Catalog column over the catalog area, under the one trigger
	// and inside THIS zone. The Models column used to be asserted here too; its routes moved to the
	// MODELS zone, so the lakehouse panel no longer carries them and this list must not either.
	for (const [row, href] of [
		['Tables', '/lakehouse/catalog/tables'],
		['Namespaces', '/lakehouse/catalog/namespaces'],
		['Warehouses', '/lakehouse/catalog/warehouses'],
		['Storage', '/lakehouse/catalog/storage/'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
	// NO `Projects` row — INVERTED BY RULING (2026-08-03, f5dd1f0). This list used to lead with
	// Projects → /lakehouse/catalog/projects. There is ONE project concept and it is the TOP of the
	// hierarchy (project > warehouse > namespace > table); a tenants list nested inside a
	// project-scoped zone's own dropdown inverted that hierarchy. The IA round then FINISHED the move:
	// the route itself is deleted and the surface is the home zone's `/projects`, so this href
	// resolves nowhere at all. The assertion stays as the guard against the dead row returning.
	await expect(panel.locator('a[href="/lakehouse/catalog/projects"]')).toHaveCount(0);
	// These rows used to leave this zone's route manifest and had to hard-navigate. Since the catalog,
	// lineage, models and admin areas merged into ONE zone they are same-zone soft navigations, and the
	// shell must NOT force a document reload on them — that is the whole payoff of the merge.
	await expect(panel.locator('a[href="/lakehouse/catalog/tables"]')).not.toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);

	// Escape closes it, and the trigger is still the focused element.
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	// Keyboard-only open: focus the trigger and press Enter.
	await lakehouse.focus();
	await page.keyboard.press('Enter');
	await expect(page.locator('[data-slot="navigation-menu-viewport"]')).toBeVisible();
});

test('a row that genuinely leaves the zone still hard-navigates', async ({ page }) => {
	// Cross-zone links must force a document load: a soft nav would route into a manifest this zone
	// does not own, i.e. a 404. Two shapes to pin, because the bar now has both: a panel ROW (Search's
	// media rows) and a top-level plain LINK (Annotate, whose zone has a single surface).
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/lineage');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// The href carries a TRAILING SLASH (1fe9439, pinned by @rask/zone-contract's nav-truth suite):
	// each zone's `paths.base` serves the trailing form, so a bare `/annotator` costs the browser a
	// 308 on every cross-zone hop. This locator asserted the bare form and so stopped matching at all
	// — a missing element passes nothing, it just reports "not found".
	await expect(nav.locator('a[href="/annotator/"]')).toHaveAttribute('data-sveltekit-reload', '');
	await openPanel(page, 'Search');
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(panel.locator('a[href="/explorer/atlas"]')).toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
});

test('a panel reaches its own zone root exactly once — the trigger is a button, not a link', async ({
	page,
}) => {
	// The trigger opens a panel instead of navigating, so a reachable root has to be a ROW. Lineage is
	// a column of Lakehouse's panel and its Graph row IS the area root (/lakehouse/lineage), so the
	// panel must not also synthesize a header row for it — exactly one link, no duplicate.
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/lineage');
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.locator('a[href="/lakehouse/lineage"]')).toHaveCount(1);
	await expect(panel.getByRole('link', { name: /^Graph/ })).toHaveAttribute(
		'href',
		'/lakehouse/lineage',
	);
	for (const [row, href] of [
		['Datasets', '/lakehouse/lineage/datasets'],
		['Jobs', '/lakehouse/lineage/jobs'],
		['Runs', '/lakehouse/lineage/runs'],
		['Columns', '/lakehouse/lineage/columns'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
});

test('the loading skeleton reserves the resolved entry widths — no shift when /v1/me lands', async ({
	page,
}) => {
	// The 4b2af0e no-CLS contract, re-proven against the NavigationMenu chrome: the skeleton pills
	// must reserve the resolved entries' exact box, chevrons included, so the navbar does not jump
	// when the identity resolves. Held open until the loading geometry has been measured.
	//
	// Resolved as a NON-ADMIN, and the contract is whole again for BOTH identities. It briefly could
	// not be: while Settings rode the zone bar, an admin's row genuinely grew by one trigger when
	// /v1/me landed. The two-level ruling moved Settings to the main menu, so the in-project bar is
	// identity-independent once more and nothing is earned by privilege here at all.
	//
	// The shift this now catches is the TIER GAP: the skeleton must reserve it exactly as the resolved
	// bar renders it. It did not, at first — the row measured 26px narrower while /v1/me was in flight
	// and snapped wider the instant it resolved. Chrome is chrome whether or not the identity has
	// landed.
	let release = () => {};
	const gate = new Promise<void>((resolve) => (release = resolve));
	await page.route('**/capi/v1/me', async (route) => {
		await gate;
		return json(route, {
			sub: 'user:bob',
			name: 'Bob',
			email: 'bob@example.com',
			estate_admin: false,
			projects: [{ project: 'acme', role: 'member' }],
		});
	});
	await page.goto('/lakehouse/lineage');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	await expect(nav.locator('[data-slot="skeleton"]').first()).toBeVisible();
	// Measure only once the SHELL around the navbar has finished laying out. The skeleton is in the
	// SSR markup, so it is visible before hydration has sized the sidebar — sampling there records
	// an x that is one sidebar-width (256px) off and reports it as a navbar shift that never
	// happened. Settling first keeps the assertion below exact (<=1px) instead of padding it.
	await expect(page.locator('[data-sidebar="content"]')).toBeVisible();
	const settled = async () => {
		let last = -1;
		await expect
			.poll(
				async () => {
					const x = (await nav.boundingBox())!.x;
					const stable = x === last;
					last = x;
					return stable;
				},
				{ timeout: 15_000 },
			)
			.toBe(true);
	};
	await settled();
	const loading = (await nav.boundingBox())!;

	release();
	await expect(nav.getByRole('button', { name: 'Lakehouse', exact: true })).toBeVisible();
	// The resolved bar carries exactly the triggers the skeleton reserved: for THIS identity no entry
	// is earned, so the row cannot grow when /v1/me lands — which is the shift this test measures.
	for (const trigger of ['Lakehouse', 'Compute', 'Search']) {
		await expect(nav.getByRole('button', { name: trigger, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('button')).toHaveCount(3);
	// Fail-closed, restated at the exact moment it matters: a resolved NON-admin gets no Settings.
	await expect(nav.getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
	const resolved = (await nav.boundingBox())!;
	expect(Math.abs(resolved.width - loading.width)).toBeLessThanOrEqual(1);
	expect(Math.abs(resolved.x - loading.x)).toBeLessThanOrEqual(1);
});

test('the breadcrumb bar is its OWN row below the zone links — never overlapping, even narrow', async ({
	page,
}) => {
	// The regression this guards: both used to share one header row, so on a narrow viewport the
	// trail and the zone links collided. Geometry, not markup: the breadcrumb row must start at or
	// below the navbar row's bottom edge, and the two must not intersect vertically.
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.setViewportSize({ width: 820, height: 800 });
	await page.goto('/lakehouse/lineage/datasets');
	const zones = page.getByRole('navigation', { name: 'Zones' });
	const trail = page.getByRole('navigation', { name: 'Breadcrumb' });
	await expect(zones).toBeVisible();
	await expect(trail).toBeVisible();
	const zonesBox = (await zones.boundingBox())!;
	const trailBox = (await trail.boundingBox())!;
	expect(trailBox.y).toBeGreaterThanOrEqual(zonesBox.y + zonesBox.height - 1);
	// …and neither row overflows the viewport horizontally.
	expect(zonesBox.x + zonesBox.width).toBeLessThanOrEqual(820);
	expect(trailBox.x + trailBox.width).toBeLessThanOrEqual(820);
});

test('the breadcrumb trail links back up the path and marks the current page', async ({ page }) => {
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/lineage/datasets/silver%24features');
	const trail = page.getByRole('navigation', { name: 'Breadcrumb' });
	// Every crumb but the last is a link to its own path prefix…
	await expect(trail.locator('a[href="/lakehouse/lineage"]')).toBeVisible();
	await expect(trail.locator('a[href="/lakehouse/lineage/datasets"]')).toBeVisible();
	// …and the leaf is the current page: text (not a link) and percent-decoded for reading.
	await expect(trail.locator('[aria-current="page"]')).toHaveText('silver$features');
	await expect(trail.locator('a[href*="silver"]')).toHaveCount(0);
});
