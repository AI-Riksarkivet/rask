import { test, expect, type Page, type Route } from '@playwright/test';

// The estate shell in the LINEAGE zone: the cross-zone TopNavbar fed by a mocked /v1/me (hermetic —
// the layout fetches it through this zone's /capi/v1/me pass-through), the zone-scoped sidebar
// carrying exactly this zone's views (Datasets / Jobs / Runs / Columns + the Graph at the root), and
// the two-row header — navbar row above, breadcrumb bar below.
//
// The bar's IA (8a0fbbc) is THREE domain triggers — Lakehouse (Catalog + Models, plus Governance +
// Operations for an estate admin), Lineage, Media — never a flat list of zones. Every entry carries
// sub-areas, so every entry is a NavigationMenu trigger; Home is the product mark, not an entry.

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
	// The bar is three DOMAIN triggers and nothing else — even for an estate admin, whose extra
	// surfaces arrive as columns inside Lakehouse's panel rather than as new top-level entries.
	// All three carry sub-areas, so all three are NavigationMenu triggers (buttons), never links.
	for (const domain of ['Lakehouse', 'Search']) {
		await expect(nav.getByRole('button', { name: domain, exact: true })).toBeVisible();
	}
	// Two TRIGGERS (Lakehouse, Search) — Annotate is a plain link, not a button, because that
	// zone has a single surface and a one-row dropdown would be noise. Compute joins as a third
	// trigger when the rask zone lands.
	await expect(nav.getByRole('button')).toHaveCount(2);
	// Home is the product mark, not a nav entry. With every panel closed the ONLY link in the bar is
	// Annotate — the single-surface zone that is a plain link by design; each panel TRIGGER must stay a
	// button, or clicking it would navigate instead of opening the panel.
	await expect(nav.getByRole('link')).toHaveCount(1);
	await expect(nav.getByRole('link', { name: 'Annotate', exact: true })).toBeVisible();
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
	// The bar never advertises privilege — the same three triggers whoever is looking…
	await expect(nav.getByRole('button', { name: 'Lakehouse', exact: true })).toBeVisible();
	// Two TRIGGERS (Lakehouse, Search) — Annotate is a plain link, not a button, because that
	// zone has a single surface and a one-row dropdown would be noise. Compute joins as a third
	// trigger when the rask zone lands.
	await expect(nav.getByRole('button')).toHaveCount(2);
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

test('an estate admin gets the Governance + Operations columns inside the Lakehouse panel', async ({
	page,
}) => {
	// The positive half of the guarantee above: the admin surfaces DO exist, and they are rows in
	// Lakehouse's panel — never a top-level entry, and never anywhere else in the bar.
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
	for (const label of ['Catalog', 'Models', 'Governance', 'Operations']) {
		await expect(panel.getByText(label, { exact: true })).toBeVisible();
	}
	// Each governance/operations row links where it claims.
	for (const [row, href] of [
		['Access', '/lakehouse/admin/access'],
		['Tenants', '/lakehouse/admin/tenants'],
		['Audit', '/lakehouse/admin/audit'],
		['Events', '/lakehouse/admin/events'],
		['Streams', '/lakehouse/admin/streams'],
		['DLQ', '/lakehouse/admin/dlq'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
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
	// The rows link where they claim — the Catalog column over the data area, the Models column over
	// the models area, both under the one trigger and both inside THIS zone.
	for (const [row, href] of [
		['Projects', '/lakehouse/data/projects'],
		['Tables', '/lakehouse/data/tables'],
		['Namespaces', '/lakehouse/data/namespaces'],
		['Warehouses', '/lakehouse/data/warehouses'],
		['Registry', '/lakehouse/models'],
		['Experiments', '/lakehouse/models/experiments'],
		['Pipeline', '/lakehouse/models/pipeline'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
	// These rows used to leave this zone's route manifest and had to hard-navigate. Since the catalog,
	// lineage, models and admin areas merged into ONE zone they are same-zone soft navigations, and the
	// shell must NOT force a document reload on them — that is the whole payoff of the merge.
	await expect(panel.locator('a[href="/lakehouse/data/tables"]')).not.toHaveAttribute(
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
	await expect(nav.locator('a[href="/annotator"]')).toHaveAttribute('data-sveltekit-reload', '');
	await openPanel(page, 'Search');
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(panel.locator('a[href="/media/atlas"]')).toHaveAttribute(
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
	// Resolved as an ESTATE ADMIN on purpose — that is the only identity whose IA differs, and
	// under the three-trigger bar its extra surfaces must land as columns INSIDE the Lakehouse
	// panel. So the admin case is exactly the one that would shift the bar if the panel content
	// ever leaked back out into a top-level entry, which makes it the strongest no-shift probe.
	let release = () => {};
	const gate = new Promise<void>((resolve) => (release = resolve));
	await page.route('**/capi/v1/me', async (route) => {
		await gate;
		return json(route, {
			sub: 'user:alice',
			name: 'Alice',
			email: 'alice@example.com',
			estate_admin: true,
			projects: [{ project: 'acme', role: 'admin' }],
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
	// The resolved bar carries the same three triggers the skeleton reserved — no earned entry.
	// Two TRIGGERS (Lakehouse, Search) — Annotate is a plain link, not a button, because that
	// zone has a single surface and a one-row dropdown would be noise. Compute joins as a third
	// trigger when the rask zone lands.
	await expect(nav.getByRole('button')).toHaveCount(2);
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
