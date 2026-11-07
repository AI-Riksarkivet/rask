import { expect, test, type Page } from '@playwright/test';
import { ME_ADMIN, ME_MEMBER, mockMe, signIn, TOKEN } from './session';

/** Open a navbar panel and hand back its viewport.
 *
 * On a loaded machine a click can land on a rendered trigger before bits-ui has
 * attached its handlers: the markup is inert rather than broken and the panel silently never
 * opens. Retrying the click rides out that race — what must hold (the panel DOES open, and carries
 * the rows asserted by the caller) is unchanged; only the delivery is made robust. It clicks only
 * while the panel is closed, so a retry can never toggle an already-open panel back shut. */
const openPanel = async (page: Page, name: string) => {
	const trigger = page
		.getByRole('navigation', { name: 'Pages' })
		.getByRole('button', { name, exact: true });
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(async () => {
		if (!(await panel.isVisible())) await trigger.click();
		await expect(panel).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 20_000 });
	return panel;
};

// The admin area's estate-admin door: `admin/+layout.ts` refuses with 403 before an admin page renders,
// on the `/management/v1/me` the console reads (here answered by `mockMe`, or by the mock catalog for the
// signed-in token). The door is presentation: the catalog and lineage enforce estate-admin on every read
// behind these pages. The navbar is the other layer: Operations is a COLUMN of the Lakehouse panel,
// appended only for an estate admin, asserted with the panel OPEN.

// No shared beforeEach sign-in: each test signs in AS the identity it is about.

test('a non-estate-admin sees ForbiddenPage on every admin route + no admin nav entries', async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.member });
	await mockMe(page, ME_MEMBER);
	// Every route the door covers, and it covers exactly the `admin` area now: #105 took the access
	// workbench and the audit trail to the console (`/settings/*`, behind ITS own layout gate) and
	// retired the tenants page, so `governance` is not a segment the console serves at all.
	for (const path of ['/admin/dlq', '/admin/streams', '/admin/events']) {
		await page.goto(path);
		await expect(page.getByText('Admin is estate-admin only')).toBeVisible();
		// The route's own content never renders behind the door.
		await expect(page.getByRole('heading', { name: 'Lineage DLQ' })).toHaveCount(0);
		await expect(page.getByRole('heading', { name: 'Streams' })).toHaveCount(0);
	}
	// The navbar hides the admin surfaces from a non-admin (fail-closed IA, not just the door).
	const nav = page.getByRole('navigation', { name: 'Pages' });
	// The bar itself is identity-independent — one trigger per page, no Admin entry in any shape…
	await expect(nav.getByRole('button', { name: 'Lakehouse', exact: true })).toBeVisible();
	// ONE trigger: the in-project bar is the Lakehouse entry alone.
	await expect(nav.getByRole('button')).toHaveCount(1);
	await expect(nav.getByRole('button', { name: 'Admin', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('link', { name: 'Admin', exact: true })).toHaveCount(0);
	await expect(nav.getByText('Access')).toHaveCount(0);
	// …and the real guarantee: opening the trigger that WOULD carry them shows the tighter
	// non-admin panel — Catalog, Lineage and nothing else. No Governance column, no
	// Operations column, and not one /admin row: the IA never even hints at a surface this identity
	// is barred from.
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Catalog', { exact: true })).toBeVisible();
	// Workspace is not a privilege — a non-admin gets it too. Asserted so the "tighter panel" claim
	// above names the columns that DO render, not just the ones that don't.
	await expect(panel.getByText('Workspace', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	await expect(panel.getByText('Operations', { exact: true })).toHaveCount(0);
	await expect(panel.locator('a[href^="/admin"]')).toHaveCount(0);
});

test("an estate admin passes the door and gets Lakehouse's governance columns", async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.admin });
	await mockMe(page, ME_ADMIN);
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'Lineage DLQ' })).toBeVisible();
	const nav = page.getByRole('navigation', { name: 'Pages' });
	// ONE, the same as the non-admin above: the in-project bar is identity-independent. Privilege shows
	// up one level in, as the Lakehouse panel's Operations column — never as a new destination.
	await expect(nav.getByRole('button')).toHaveCount(1);
	await expect(nav.getByRole('button', { name: 'Settings', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('button', { name: 'Admin', exact: true })).toHaveCount(0);
	// Access is NOT a top-level navbar entry, in either shape…
	await expect(nav.getByRole('link', { name: 'Access', exact: true })).toHaveCount(0);
	await expect(nav.getByRole('button', { name: 'Access', exact: true })).toHaveCount(0);
	// …it is a ROW of the Settings panel, a MAIN-MENU surface, asserted in `e2e/settings/settings.spec.ts`.
	// What remains here is the positive half of the guarantee: this identity DOES get the privileged
	// surfaces, as the app panel's Operations column, so the two tests cannot both pass on a navbar
	// that simply renders nothing.
	const panel = await openPanel(page, 'Lakehouse');
	await expect(panel.getByText('Operations', { exact: true })).toBeVisible();
	await expect(panel.getByText('Governance', { exact: true })).toHaveCount(0);
	for (const href of ['/admin/events', '/admin/streams', '/admin/dlq']) {
		await expect(panel.locator(`a[href="${href}"]`)).toBeVisible();
	}
	await page.keyboard.press('Escape');
	await expect(panel).toBeHidden();
});

test('an unresolvable identity (catalog outage) fails CLOSED, never open', async ({
	context,
	page,
}) => {
	// The catalog's /management/v1/me 502s (TOKEN.down on the mock, and the route below for good
	// measure). fetchMe returns null for ANY failure, and null is a denial — never a default-open —
	// worded as the unconfirmed identity it is, the same refusal `/settings` gives.
	await signIn(context, { token: TOKEN.down });
	await page.route('**/api/catalog/management/v1/me', (route) =>
		route.fulfill({ status: 502, contentType: 'application/json', body: '{"detail":"down"}' }),
	);
	await page.goto('/admin/dlq');
	await expect(page.getByText('403', { exact: true })).toBeVisible();
	await expect(page.getByText('could not be confirmed', { exact: false })).toBeVisible();
	await expect(page.getByRole('heading', { name: 'Lineage DLQ' })).toHaveCount(0);
});
