import { test, expect, type Page, type Route } from '@playwright/test';

// The estate shell in the MODELS zone: the cross-zone TopNavbar fed by a mocked /v1/me (hermetic —
// the layout fetches it through this zone's /capi/v1/me pass-through), and the zone-scoped sidebar
// carrying ONLY this zone's own routes.
//
// Under the three-trigger IA (8a0fbbc) this zone has no top-level entry of its own: the model
// registry is the "Models" COLUMN of the Lakehouse panel, because a model is a catalog object over
// the same estate. The bar stays three words wide however many routes the product grows.

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
	// Three domain triggers, even for an estate admin — the extra surfaces are panel columns, not
	// new entries. Every one carries sub-areas, so every one is a button; with the panels closed
	// the bar holds no links at all (Home is the product mark, not an entry).
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
	await expect(nav.getByRole('button', { name: 'Lakehouse', exact: true })).toBeVisible();
	// Two TRIGGERS (Lakehouse, Search) — Annotate is a plain link, not a button, because that
	// zone has a single surface and a one-row dropdown would be noise. Compute joins as a third
	// trigger when the rask zone lands.
	await expect(nav.getByRole('button')).toHaveCount(2);
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
	await expect(panel.locator('a[href="/lakehouse/admin/access"]')).toBeVisible();
	for (const row of [
		'/lakehouse/admin/tenants',
		'/lakehouse/admin/audit',
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
	await expect(panel.locator('a[href="/lakehouse/data/tables"]')).not.toHaveAttribute(
		'data-sveltekit-reload',
		'',
	);
});

test('the project switcher heads the navbar row — it no longer lives in the sidebar', async ({
	page,
}) => {
	await page.route('**/capi/v1/me', (route) => json(route, { detail: 'anon' }, 401));
	await page.goto('/lakehouse/models');
	const switcher = page.getByRole('button', { name: 'Switch project' });
	await expect(switcher).toBeVisible();
	// The sidebar header is gone entirely: the sidebar is in-zone routes only.
	await expect(page.locator('[data-sidebar="header"]')).toHaveCount(0);
	// It sits on the navbar row, left of the zone links.
	const switcherBox = (await switcher.boundingBox())!;
	const zonesBox = (await page.getByRole('navigation', { name: 'Zones' }).boundingBox())!;
	expect(switcherBox.x).toBeLessThan(zonesBox.x);
	expect(switcherBox.y).toBeLessThan(zonesBox.y + zonesBox.height);
});
