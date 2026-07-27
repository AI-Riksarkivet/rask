import { test, expect, type Route, type Page } from '@playwright/test';
import { AUTH_OFF } from '../ports';

// Responsive contract for the SHARED @rask/ui shell (#105). It lives in the data zone because a
// spec needs *some* zone to render the shell in, but it imports nothing zone-specific and asserts
// nothing about the data zone's own content — every assertion is about chrome that every
// microfrontend inherits from `AppShell`:
//
//   1. the document never scrolls sideways (scrollWidth <= clientWidth) at any width, so a wide
//      table scrolls INSIDE its container and page content is never sliced off the viewport;
//   2. the account control never overlaps a navbar entry — below the shell's collapse breakpoint
//      the zone links fold into a single overflow menu instead of running under the avatar.
//
// Widths span the wide bar (1440, and 1024 = the collapse boundary itself), the two widths the
// live bug was screenshotted at (900 = avatar on top of the last entry, 600 = navbar off the right
// edge + body scrolling sideways), and 768 in between.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// An estate admin: the WIDEST navbar the shell can render (the Lakehouse panel gains the
// governance + operations columns), so the overflow assertions are made against the worst case.
const ME_ADMIN = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [{ project: 'acme', role: 'admin' }],
};

// Deliberately long ids: a registry row wider than a 600px viewport is exactly what used to push
// the document sideways, so the overflow assertion has something real to contain.
const TABLES = [
	'acme-bronze$very-long-table-identifier-for-overflow-checking',
	'acme-silver$another-extremely-long-table-identifier-here',
	'acme-gold$catalog',
];

const WIDTHS = [1440, 1024, 900, 768, 600];

/** The shell's collapse breakpoint (`SHELL_COLLAPSE_BREAKPOINT`) — BELOW it the zone links live in
 *  one overflow menu and the sidebar is an off-canvas sheet; at or above it, the wide bar. */
const COLLAPSE_AT = 1024;

test.beforeEach(async ({ page }) => {
	await page.route('**/capi/**', (route) => {
		const path = new URL(route.request().url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/me') return json(route, ME_ADMIN);
		if (path === '/v1/table') return json(route, { tables: TABLES });
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

/**
 * Drive the shell on a PROJECT host (`acmecorp.localhost`), not the bare dev host. The estate's IA
 * is project-first — the project IS the request host — so the navbar's project switcher renders a
 * real label there instead of the "Default" placeholder, which is what made the row wide enough for
 * the last zone trigger to slide under the account avatar in the first place. Asserting on the bare
 * host would measure a narrower bar than any real deployment ever shows.
 */
function projectUrl(baseURL: string | undefined, path: string): string {
	const url = new URL(path, baseURL ?? AUTH_OFF);
	url.hostname = `acmecorp.${url.hostname}`;
	return url.href;
}

/** Land on a zone route with the shell fully resolved: the identity has landed (so the navbar is
 *  past its skeleton state) and the table has rendered. */
async function openShell(
	page: Page,
	baseURL: string | undefined,
	path = '/lakehouse/data/tables',
): Promise<void> {
	await page.goto(projectUrl(baseURL, path));
	await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
	await expect(page.getByRole('link', { name: TABLES[2] })).toBeVisible();
}

for (const width of WIDTHS) {
	test(`document never scrolls sideways at ${width}px (#105)`, async ({ page, baseURL }) => {
		await page.setViewportSize({ width, height: 900 });
		await openShell(page, baseURL);
		const doc = await page.evaluate(() => ({
			scrollWidth: document.documentElement.scrollWidth,
			clientWidth: document.documentElement.clientWidth,
			bodyScrollWidth: document.body.scrollWidth,
		}));
		expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth);
		expect(doc.bodyScrollWidth).toBeLessThanOrEqual(doc.clientWidth);
	});

	test(`the account control never overlaps a navbar entry at ${width}px (#105)`, async ({
		page,
		baseURL,
	}) => {
		await page.setViewportSize({ width, height: 900 });
		await openShell(page, baseURL);

		const account = await page.getByRole('button', { name: 'Account' }).boundingBox();
		expect(account).not.toBeNull();

		// Every VISIBLE navbar entry: the zone triggers/links on the desktop bar, or the single
		// overflow-menu trigger below the breakpoint.
		const entries = page.locator('[data-slot="navigation-menu-list"] > li:visible');
		const boxes = await entries.evaluateAll((els) =>
			els.map((el) => {
				const r = el.getBoundingClientRect();
				return { text: el.textContent?.trim() ?? '', ...r.toJSON() };
			}),
		);
		expect(boxes.length).toBeGreaterThan(0);
		for (const box of boxes) {
			const overlaps =
				box.left < account!.x + account!.width &&
				box.right > account!.x &&
				box.top < account!.y + account!.height &&
				box.bottom > account!.y;
			expect(overlaps, `navbar entry "${box.text}" overlaps the account control`).toBe(false);
			// An entry pushed past the right edge is the same bug with a different symptom.
			expect(box.right, `navbar entry "${box.text}" runs off the viewport`).toBeLessThanOrEqual(
				width,
			);
		}
	});
}

test('below the breakpoint the zone links collapse into one overflow menu that still reaches every destination (#105)', async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 600, height: 900 });
	await openShell(page, baseURL);

	// The desktop bar is gone: exactly one entry — the overflow trigger.
	const entries = page.locator('[data-slot="navigation-menu-list"] > li:visible');
	await expect(entries).toHaveCount(1);

	const menu = page.getByRole('button', { name: 'Menu' });
	await expect(menu).toBeVisible();
	await menu.click();

	// Every zone, and the sub-areas underneath them, stay reachable from the one menu.
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(panel.locator('[data-slot="navbar-overflow-group"]')).toHaveText([
		'Lakehouse',
		'Search',
		'Annotate',
	]);
	// One row per destination the wide bar reaches — including each zone's own root and the
	// estate-admin-only governance rows.
	for (const href of [
		'/lakehouse/data',
		'/lakehouse/data/tables',
		'/lakehouse/data/warehouses',
		'/lakehouse/models',
		'/lakehouse/lineage',
		'/lakehouse/lineage/runs',
		'/lakehouse/lineage/columns',
		'/media',
		'/annotator',
		'/lakehouse/admin/access',
		'/lakehouse/admin/dlq',
	]) {
		await expect(panel.locator(`a[href="${href}"]`)).toHaveCount(1);
	}
	// The panel itself stays inside the viewport — an overflow menu that hangs off the edge is the
	// bug wearing a different hat.
	const box = await panel.boundingBox();
	expect(box!.x).toBeGreaterThanOrEqual(0);
	expect(box!.x + box!.width).toBeLessThanOrEqual(600);
});

test('above the breakpoint the zone links stay on the bar (no overflow menu) (#105)', async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page, baseURL);
	await expect(page.getByRole('button', { name: 'Menu' })).toHaveCount(0);
	// Panel triggers are buttons; Annotate is a plain link (single-surface zone), so assert by role.
	for (const label of ['Lakehouse', 'Search']) {
		await expect(page.getByRole('button', { name: label })).toBeVisible();
	}
	await expect(page.getByRole('link', { name: 'Annotate', exact: true })).toBeVisible();
});

test('the wide Lakehouse panel offers a way into the zone ROOT, not only its sub-areas', async ({
	page,
	baseURL,
}) => {
	// The `groups` branch shipped without the zone-root row that the `items` branch renders and its own
	// comment declares mandatory ("a panel must not be the only way in, and the trigger is a button, not a
	// link"). Consequence, on every wide screen: /lakehouse/data was reachable by breadcrumb or by typing
	// the URL, and by nothing you could click. The narrow bar asserted this all along (the overflow test
	// above lists /lakehouse/data); the wide bar only asserted that the trigger was VISIBLE, so opening the
	// panel — the thing a user actually does — was never checked.
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page, baseURL);
	await page.getByRole('button', { name: 'Lakehouse' }).click();
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(panel.locator('a[href="/lakehouse/data"]')).toHaveCount(1);
	// The trigger stays a button (that is the whole reason the row is needed), so this is not a regression
	// to a link-trigger — the panel row is the way in.
	await expect(page.getByRole('button', { name: 'Lakehouse' })).toBeVisible();

	// …and the row must not COST the columns. The first attempt at this fix added the row as a sibling
	// before the grid, which gave NavigationMenu.Content two children; bits-ui sizes the shared viewport
	// from the active content, so it measured 69px — the row's own height — and clipped all five columns.
	// The href assertion above passed anyway (every link was still in the DOM, and Playwright reported
	// them visible: the clip is on an ancestor). A screenshot is what caught it. So assert the rendered
	// GEOMETRY, which is the thing that was wrong.
	const box = await panel.boundingBox();
	expect(box!.height, 'the panel is clipped — the group columns are not showing').toBeGreaterThan(
		200,
	);
	// And assert on innerText, not textContent: textContent returns every panel's markup including the
	// inactive siblings', which is how "alice sees the Governance column" passed against a 69px panel.
	// innerText reflects RENDERED text, and the group labels are CSS-uppercased, so compare case-insensitively.
	const visible = (await panel.innerText()).toLowerCase();
	for (const group of ['catalog', 'models', 'lineage']) {
		expect(visible, `the ${group} column is not visible in the panel`).toContain(group);
	}
});

test('the breadcrumb truncates MIDDLE segments and always shows the current page (#105)', async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 600, height: 900 });
	await page.goto(projectUrl(baseURL, `/lakehouse/data/tables/${encodeURIComponent(TABLES[0])}`));
	const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' });
	// The current page is the LAST crumb and is never the one that gets dropped (the label is the
	// humanised segment — `pathCrumbs` percent-decodes and un-dashes it for display).
	const current = crumbs.locator('[data-slot="breadcrumb-page"]');
	await expect(current).toBeVisible();
	await expect(current).toHaveText(TABLES[0].replace(/-/g, ' '));
	// A middle segment is folded behind an ellipsis affordance rather than squeezing the trail…
	const ellipsis = crumbs.locator('[data-slot="breadcrumb-ellipsis"]');
	await expect(ellipsis).toBeVisible();
	// …and folding hides it from the ROW, not from the user: the skipped ancestor is still one click
	// away, so the trail stays navigable.
	await ellipsis.click();
	await expect(page.getByRole('menuitem').getByRole('link', { name: 'tables' })).toBeVisible();
	await page.keyboard.press('Escape');
	// The breadcrumb bar itself never overflows its row.
	const fits = await crumbs.evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
	expect(fits).toBe(true);
});

test(`the sidebar is off-canvas below ${COLLAPSE_AT}px and on-canvas above it (#105)`, async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page, baseURL);
	// Desktop: the zone nav is laid out in the document.
	await expect(page.locator('[data-slot="sidebar"][data-state="expanded"]')).toBeVisible();

	await page.setViewportSize({ width: 600, height: 900 });
	// Mobile: no in-document sidebar at all — it becomes a sheet behind the trigger.
	await expect(page.locator('[data-slot="sidebar"][data-state]')).toHaveCount(0);
	await page.getByRole('button', { name: 'Toggle Sidebar' }).click();
	await expect(page.locator('[data-sidebar="sidebar"][data-mobile="true"]')).toBeVisible();
});

test('a table wider than the viewport scrolls INSIDE its card (#105)', async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 600, height: 900 });
	await openShell(page, baseURL);
	const card = page.locator('[data-slot="data-table-card"]');
	await expect(card).toBeVisible();
	const measured = await card.evaluate((el) => {
		const scroller = el.querySelector('[data-slot="table-container"]')!;
		return {
			cardWidth: el.getBoundingClientRect().width,
			cardRight: el.getBoundingClientRect().right,
			scrolls: scroller.scrollWidth > scroller.clientWidth,
		};
	});
	// The card is bounded by the viewport…
	expect(measured.cardWidth).toBeLessThanOrEqual(600);
	expect(measured.cardRight).toBeLessThanOrEqual(600);
	// …the wide table scrolls inside it…
	expect(measured.scrolls).toBe(true);
	// …and the document is never pushed sideways instead.
	const doc = await page.evaluate(() => document.documentElement.scrollWidth);
	expect(doc).toBeLessThanOrEqual(600);
});
