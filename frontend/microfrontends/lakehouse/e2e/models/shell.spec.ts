import { test, expect, type Page, type Route } from '@playwright/test';

// The estate shell in the MODELS zone: the cross-zone TopNavbar fed by a mocked /v1/me (hermetic —
// the layout fetches it through this zone's /capi/v1/me pass-through), and the zone-scoped sidebar
// carrying ONLY this zone's own routes.
//
// Under the one-entry-per-ZONE IA (R15) this area has no top-level entry of its own: the model
// registry is the "Models" COLUMN of the Lakehouse panel, because a model is a catalog object over
// the same estate. The bar grows only when a ZONE does — a new route is a row in a column.
//
// The bar's triggers (zones owning sub-areas) are Lakehouse, Compute and Search; its plain links
// (single-surface zones) are Workbench, Annotate, Train and Studio. Both sets are asserted BY NAME
// below: a bare `toHaveCount(2)` is what let these assertions rot silently through the seven-zone IA
// (db15ea8, which gave Compute its panel) and the workbench zone (117c8ed, the 8th zone).

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

const ADMIN = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

test('an estate admin gets the zone triggers + the models sidebar leaves', async ({ page }) => {
	await page.route('**/capi/v1/me', (route) => json(route, ADMIN));
	await page.goto('/lakehouse/models');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// The same triggers for an estate admin as for anyone else — the extra surfaces are panel columns,
	// not new entries. Named AND counted: the names catch a zone silently leaving the bar (the R15
	// defect), the count catches one silently joining it.
	for (const trigger of ['Lakehouse', 'Compute', 'Search']) {
		await expect(nav.getByRole('button', { name: trigger, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('button')).toHaveCount(3);
	// Home is the product mark, not a nav entry. With every panel closed the bar's LINKS are exactly
	// the single-surface zones — one surface means a one-row dropdown would be noise; each panel
	// TRIGGER must stay a button, or clicking it would navigate instead of opening the panel.
	for (const link of ['Workbench', 'Annotate', 'Train', 'Studio']) {
		await expect(nav.getByRole('link', { name: link, exact: true })).toBeVisible();
	}
	await expect(nav.getByRole('link')).toHaveCount(4);
	// This zone is NOT its own entry any more, and Access is not one either (in any shape).
	await expect(nav.getByRole('link', { name: 'Models', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Access', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('button', { name: 'Access', exact: true })).toHaveCount(0);
	// The sidebar renders ONLY this zone's own routes.
	await expect(page.locator('[data-active="true"]').filter({ hasText: 'Registry' })).toBeVisible();
	await expect(page.getByRole('link', { name: 'Pipeline' })).toBeVisible();
	await expect(page.getByRole('link', { name: 'Experiments' })).toBeVisible();
});

test('a signed-out / unresolved identity gets no governance column (fail-closed)', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/models');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// The bar looks identical whoever is looking — privilege shows up only INSIDE the panel, so
	// that is where the fail-closed assertion has to bite.
	for (const trigger of ['Lakehouse', 'Compute', 'Search']) {
		await expect(nav.getByRole('button', { name: trigger, exact: true })).toBeVisible();
	}
	// The same NUMBER of triggers as the admin case above: a privileged surface must never earn a
	// top-level entry (see the file header on why the name list rides beside the count).
	await expect(nav.getByRole('button')).toHaveCount(3);
	await expect(nav.getByText('Access')).toHaveCount(0);
	const panel = await openPanel(page, 'Lakehouse');
	// The non-admin panel is the tighter two-column one: catalog + models, nothing governing.
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Models', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/admin"]')).toHaveCount(0);
});

test("Access is reachable only from Lakehouse's Governance column, never as its own navbar entry", async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) => json(route, ADMIN));
	await page.goto('/lakehouse/models');
	const nav = page.getByRole('navigation', { name: 'Zones' });
	const panel = await openPanel(page, 'Lakehouse');
	// Access rides in Governance, alongside the rest of the estate-admin surfaces…
	await expect(panel.getByText('Governance', { exact: true })).toBeVisible();
	await expect(panel.getByText('Operations', { exact: true })).toBeVisible();
	await expect(panel.locator('a[href="/lakehouse/governance/access"]')).toBeVisible();
	for (const row of [
		'/lakehouse/admin/tenants',
		'/lakehouse/governance/audit',
		'/lakehouse/admin/streams',
		'/lakehouse/admin/dlq',
	]) {
		await expect(panel.locator(`a[href="${row}"]`)).toBeVisible();
	}
	// …and with the panel closed again the navbar row itself carries no Access entry of any kind.
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	await expect(nav.getByText('Access')).toHaveCount(0);
});

test('this zone is a ROW of the Lakehouse panel, and its rows link where they claim', async ({
	page,
}) => {
	// The growth rule the IA rests on: a new route becomes a row in a column, never a new top-level
	// entry. The models zone is the first zone to have been folded in that way, so assert its rows
	// actually resolve to this zone rather than merely being labelled for it.
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/models');
	const panel = await openPanel(page, 'Lakehouse');
	for (const [row, href] of [
		['Registry', '/lakehouse/models'],
		['Experiments', '/lakehouse/models/experiments'],
		['Pipeline', '/lakehouse/models/pipeline'],
	] as const) {
		await expect(panel.getByRole('link', { name: new RegExp(`^${row}`) })).toHaveAttribute(
			'href',
			href,
		);
	}
	// Registry IS this zone's root, so it is reachable exactly once — no duplicate synthesized row.
	await expect(panel.locator('a[href="/lakehouse/models"]')).toHaveCount(1);
	// EVERY row in this panel now stays inside this app's route manifest — the catalog rows used to
	// cross a zone boundary and carry a reload marker, and after the merge they must not.
	await expect(panel.locator('a[href="/lakehouse/models/pipeline"]')).not.toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
	await expect(panel.locator('a[href="/lakehouse/catalog/tables"]')).not.toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
});

test('the project switcher IS the sidebar header — exactly one control, in the project-context slot', async ({
	page,
}) => {
	// INVERTED (1927ea8 → c74e4b2, "the sidebar header IS the project dropdown"). This test used to
	// demand `[data-sidebar="header"]` count 0, from the brief period when the switcher was hoisted
	// onto the navbar row. That left the rail's most valuable slot printing the ZONE name — which the
	// navbar highlight and the breadcrumb already say twice — so the switcher went back to the header
	// and the header became nothing BUT the switcher (which project you are in, the projects you can
	// move to, and the way back to the main menu, in one control). app-shell.svelte's row-1 comment is
	// the product-side statement of the same thing.
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/models');
	const switcher = page.getByRole('button', { name: 'Switch project' });
	await expect(switcher).toBeVisible();
	// It lives in the sidebar header…
	const header = page.locator('[data-sidebar="header"]');
	await expect(header).toHaveCount(1);
	await expect(header.getByRole('button', { name: 'Switch project' })).toBeVisible();
	// …and ONLY there. Two switchers is the regression this now pins: the navbar copy is what the old
	// assertion was written for, and a shell carrying both would satisfy every other check in this file.
	await expect(page.getByRole('button', { name: 'Switch project' })).toHaveCount(1);
	// Geometry: it heads the rail — above the zone routes, and the rail is left of the navbar's zone
	// links, so the switcher still reads as global context rather than as one more in-zone route.
	const switcherBox = (await switcher.boundingBox())!;
	const zonesBox = (await page.getByRole('navigation', { name: 'Zones' }).boundingBox())!;
	const navBox = (await page.locator('[data-sidebar="content"]').boundingBox())!;
	expect(switcherBox.x).toBeLessThan(zonesBox.x);
	expect(switcherBox.y).toBeLessThan(navBox.y);
});
