import { expect, test, type Page } from '@playwright/test';
import { ME_ADMIN, ME_MEMBER, mockMe, signIn, TOKEN } from './session';

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

// The admin area's estate-admin door — now TWO doors, and this pins both.
//
//  1. admin/+layout.server.ts throws 403 before a single admin component is rendered or sent. It reads
//     /v1/me from the catalog with the session bearer, so it is driven here by WHICH TOKEN signIn mints
//     (page.route cannot reach a server-side fetch — that is the point of it being server-side).
//  2. the root +layout.svelte still refuses to render admin content client-side, driven by mockMe.
//
// A spec sets both, and they agree; a real non-admin would fail door 1 and never reach door 2. The
// navbar IA is the third layer: an identity the doors refuse is never even shown the routes.
//
// Under the three-trigger IA (8a0fbbc) this zone has no top-level entry: its surfaces are the
// Governance and Operations COLUMNS of the Lakehouse panel, appended only for an estate admin. So
// the IA half of the guarantee moved one level in — from "no Admin button in the bar" to "those
// columns do not render, and no /admin row is reachable" — and it is asserted with the panel OPEN,
// which is the only place the difference is now observable.

// No shared beforeEach sign-in: each test signs in AS the identity it is about, because the token is
// what the server-side door reads.

test('a non-estate-admin sees ForbiddenPage on every admin route + no admin nav entries', async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.member });
	await mockMe(page, ME_MEMBER);
	for (const path of [
		'/lakehouse/admin/tenants',
		'/lakehouse/governance/audit',
		'/lakehouse/admin/dlq',
	]) {
		await page.goto(path);
		await expect(page.getByText('Admin is estate-admin only')).toBeVisible();
		// The route's own content never renders behind the door.
		await expect(page.getByRole('heading', { name: 'Audit log' })).toHaveCount(0);
	}
	// The navbar hides the admin surfaces from a non-admin (fail-closed IA, not just the door).
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// The bar itself is identity-independent — one trigger per zone, no Admin entry in any shape…
	await expect(nav.getByRole('button', { name: 'Lakehouse', exact: true })).toBeVisible();
	// THREE triggers (Lakehouse, Compute, Search) — the ones whose zone owns several sub-areas.
	// Annotate, Workbench, Train and Studio are plain links, not buttons: a single-surface zone with a
	// one-row dropdown would be noise. This said TWO and named the third as pending ("Compute joins
	// when the rask zone lands"); the rask zone DID land (R16/db15ea8 — the overview zone folded into
	// Compute and it carries COMPUTE_ITEMS), so the pending clause is now the stale half. The
	// 8th zone (workbench, 117c8ed) adds a LINK, not a trigger, so it does not move this number.
	// Ground truth for the whole bar is home/e2e/auth.spec.ts's ZONE_ENTRIES, keyed off zoneDirs().
	await expect(nav.getByRole('button')).toHaveCount(3);
	await expect(nav.getByRole('button', { name: 'Admin', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Admin', exact: true })).toHaveCount(0);
	await expect(nav.getByText('Access')).toHaveCount(0);
	// …and the real guarantee: opening the trigger that WOULD carry them shows the tighter
	// non-admin panel — Catalog, Models, Lineage and nothing else. No Governance column, no
	// Operations column, and not one /admin row: the IA never even hints at a surface this identity
	// is barred from.
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(panel.getByText('Models', { exact: true })).toBeVisible();
	// Lineage is an AREA of this zone, not a privilege — a non-admin gets it too. Asserted so the
	// "tighter panel" claim above names the columns that DO render, not just the ones that don't.
	await expect(panel.getByText('Lineage', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/lakehouse/admin"]')).toHaveCount(0);
});

test("an estate admin passes the door and gets Lakehouse's governance columns", async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.admin });
	await mockMe(page, ME_ADMIN);
	await page.route('**/api/projects*', (route) =>
		route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
	);
	await page.goto('/lakehouse/admin/tenants');
	await expect(page.getByRole('heading', { name: 'Tenants' })).toBeVisible();
	const nav = page.getByRole('navigation', { name: 'Zones' });
	// The non-admin's three zone triggers (Lakehouse, Compute, Search) PLUS Settings — the ONE entry
	// privilege earns, and it earns it because estate-wide configuration is not a zone's feature
	// (2026-08-03 ruling moved governance out of the Lakehouse panel). Everything else an admin gains
	// is still a panel COLUMN. That is the whole shape of this pair of tests, and it only bites while
	// both sides assert their number: the non-admin above asserts 3 and this asserts 4, so a bar that
	// rendered Settings for everyone, or for no one, fails on one side or the other.
	await expect(nav.getByRole('button')).toHaveCount(4);
	await expect(nav.getByRole('button', { name: 'Settings', exact: true })).toBeVisible();
	await expect(nav.getByRole('button', { name: 'Admin', exact: true })).toHaveCount(0);
	// Access is NOT a top-level navbar entry, in either shape…
	await expect(nav.getByRole('link', { name: 'Access', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('button', { name: 'Access', exact: true })).toHaveCount(0);
	// …it is a ROW of the Settings panel, which this identity DOES get. Asserted with the panel open —
	// the positive half of the non-admin guarantee above, so the two tests cannot both pass on a navbar
	// that simply renders nothing.
	const settings = await openPanel(page, 'Settings');
	for (const href of [
		'/lakehouse/governance/access',
		'/lakehouse/admin/tenants',
		'/lakehouse/governance/audit',
	]) {
		await expect(settings.locator(`a[href="${href}"]`)).toBeVisible();
	}
	await page.keyboard.press('Escape');
	await expect(settings).toBeHidden();
	// …while OPERATIONS — running the estate rather than governing it — stays in the zone's own panel.
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Operations', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	for (const href of [
		'/lakehouse/admin/events',
		'/lakehouse/admin/streams',
		'/lakehouse/admin/dlq',
	]) {
		await expect(panel.locator(`a[href="${href}"]`)).toBeVisible();
	}
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
	// …and it is this zone's own sidebar leaf too, which is how it is reached from inside /admin.
	await expect(
		page.locator('[data-sidebar="content"]').getByRole('link', { name: 'Access' }),
	).toBeVisible();
});

test('an unresolvable identity (catalog outage) fails CLOSED, never open', async ({
	context,
	page,
}) => {
	// Both doors see the outage: the server's /v1/me 502s (TOKEN.down) and the browser's does too.
	// fetchMe returns null for ANY failure, and null is a denial — never a default-open.
	await signIn(context, { token: TOKEN.down });
	await page.route('**/capi/v1/me', (route) =>
		route.fulfill({ status: 502, contentType: 'application/json', body: '{"detail":"down"}' }),
	);
	await page.goto('/lakehouse/admin/tenants');
	await expect(page.getByText('Admin is estate-admin only')).toBeVisible();
});

test('the SERVER door refuses even when the browser claims to be an admin', async ({
	context,
	page,
}) => {
	// The reason door 1 exists. A member's session with a page.route that answers estate_admin=true —
	// i.e. a browser lying about its own identity — must still get 403, because the door that decides
	// never asked the browser.
	await signIn(context, { token: TOKEN.member });
	await mockMe(page, ME_ADMIN);
	const res = await page.goto('/lakehouse/admin/tenants');
	expect(res?.status()).toBe(403);
	await expect(page.getByRole('heading', { name: 'Tenants' })).toHaveCount(0);
});
