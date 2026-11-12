import { test, expect, type Route, type Page } from '@playwright/test';
import { MOCK_CATALOG } from '../admin/ports';

// Responsive contract for the SHARED @rask/ui shell (#105). It lives in the console because a
// spec needs *some* page to render the shell in, but it imports nothing page-specific and asserts
// nothing about the console's own content — every assertion is about chrome that every
// app inherits from `AppShell`:
//
//   1. the document never scrolls sideways (scrollWidth <= clientWidth) at any width, so a wide
//      table scrolls INSIDE its container and page content is never sliced off the viewport;
//   2. the account control never overlaps the breadcrumb — the top bar's one variable-width entry;
//   3. every page carries the same sidebar, whatever area it is in.
//
// Widths span the wide layout (1440, and 1024 = the collapse boundary itself), 900 and 600 (the widths
// a sideways-scrolling body was first screenshotted at), and 768 in between.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

// An estate admin: every sidebar row is a live link, the fullest rail the shell renders.
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
] as const;

const WIDTHS = [1440, 1024, 900, 768, 600];

/** The shell's collapse breakpoint (`SHELL_COLLAPSE_BREAKPOINT`) — BELOW it the sidebar is an
 *  off-canvas sheet; at or above it, a rail in the document. */
const COLLAPSE_AT = 1024;

test.beforeEach(async ({ page }) => {
	await page.route('**/api/catalog/**', (route) => {
		const path = new URL(route.request().url()).pathname.replace(/^.*\/api\/catalog/, '');
		if (path === '/management/v1/me') return json(route, ME_ADMIN);
		return route.fallback();
	});
	// Everything but `me` falls through to the mock catalog. This is the AUTH-OFF preview: no session,
	// so the bearer is "" — the one spec allowed to seed that shared bucket. The deliberately LONG table
	// ids are what stress the layout.
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: { bearer: '', routes: { 'GET /v1/table': { tables: TABLES } } },
	});
});

/** Land on a page route with the shell fully resolved: the identity has landed (so the account
 *  control is past its skeleton state) and, on the tables list, the rows have rendered. */
async function openShell(page: Page, path = '/catalog/tables'): Promise<void> {
	await page.goto(path);
	await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
	if (path === '/catalog/tables')
		await expect(page.getByRole('link', { name: TABLES[2] })).toBeVisible();
}

for (const width of WIDTHS) {
	test(`document never scrolls sideways at ${width}px (#105)`, async ({ page }) => {
		await page.setViewportSize({ width, height: 900 });
		await openShell(page);
		const doc = await page.evaluate(() => ({
			scrollWidth: document.documentElement.scrollWidth,
			clientWidth: document.documentElement.clientWidth,
			bodyScrollWidth: document.body.scrollWidth,
		}));
		expect(doc.scrollWidth).toBeLessThanOrEqual(doc.clientWidth);
		expect(doc.bodyScrollWidth).toBeLessThanOrEqual(doc.clientWidth);
	});

	test(`the account control never overlaps the breadcrumb at ${width}px (#105)`, async ({
		page,
	}) => {
		await page.setViewportSize({ width, height: 900 });
		await openShell(page, `/catalog/tables/${encodeURIComponent(TABLES[0])}`);
		const account = await page.getByRole('button', { name: 'Account' }).boundingBox();
		const crumbs = await page.getByRole('navigation', { name: 'Breadcrumb' }).boundingBox();
		expect(account).not.toBeNull();
		expect(crumbs).not.toBeNull();
		expect(
			crumbs!.x + crumbs!.width,
			'the breadcrumb runs under the account control',
		).toBeLessThanOrEqual(account!.x);
		expect(
			account!.x + account!.width,
			'the account control runs off the viewport',
		).toBeLessThanOrEqual(width);
	});
}

/** What a sidebar offers, in order: each row's name and, for a link, where it goes. */
async function sidebarRows(page: Page): Promise<string[]> {
	const nav = page.getByRole('navigation', { name: 'Pages' });
	await expect(nav.getByRole('link', { name: 'Tables', exact: true })).toBeVisible();
	return nav
		.locator('a, [aria-disabled="true"]')
		.evaluateAll((els) =>
			els.map((el) => `${el.textContent?.trim() ?? ''} ${el.getAttribute('href') ?? '(disabled)'}`),
		);
}

test('every area carries the same sidebar: the estate pages, the catalog, the admin pages and the workbench', async ({
	page,
}) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page, '/');
	const home = await sidebarRows(page);
	for (const row of [
		'Home /',
		'Projects /projects',
		'Tables /catalog/tables',
		'Events /admin/events',
		'Workbench /workbench',
		'Settings /settings',
	]) {
		expect(home).toContain(row);
	}
	for (const path of ['/projects', '/catalog/tables', '/admin/events', '/workbench']) {
		await page.goto(path);
		await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
		// Settings' child rows show while you are inside Settings only; elsewhere the rail is identical.
		expect(await sidebarRows(page), path).toEqual(home);
	}
	// One header row: the rail's toggle, the breadcrumb, the bell and the account menu, and no second
	// bar of page links.
	const header = page.locator('header');
	await expect(header.getByRole('navigation')).toHaveCount(1);
	await expect(header.getByRole('navigation', { name: 'Breadcrumb' })).toBeVisible();
});

test('/catalog has no page of its own: it lands on the tables list, and its crumb names it unlinked', async ({
	page,
}) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await page.goto('/catalog');
	await expect(page).toHaveURL(/\/catalog\/tables$/);
	await expect(page.getByRole('link', { name: TABLES[2] })).toBeVisible();

	// A group's crumb places the reader without sending them to a page they did not ask for; the
	// page crumbs link, with the sidebar's names.
	await page.goto('/catalog/warehouses');
	const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' });
	await expect(crumbs.getByText('Catalog', { exact: true })).toBeVisible();
	await expect(crumbs.getByRole('link', { name: 'Catalog' })).toHaveCount(0);
	await page.goto(`/catalog/tables/${encodeURIComponent(TABLES[2])}`);
	await expect(crumbs.getByRole('link', { name: 'Tables', exact: true })).toHaveAttribute(
		'href',
		'/catalog/tables',
	);
});

test('the breadcrumb truncates MIDDLE segments and always shows the current page (#105)', async ({
	page,
}) => {
	await page.setViewportSize({ width: 600, height: 900 });
	await page.goto(`/catalog/tables/${encodeURIComponent(TABLES[0])}`);
	const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' });
	// The current page is the LAST crumb and is never the one that gets dropped (the label is the
	// table id, percent-decoded and otherwise as written).
	const current = crumbs.locator('[data-slot="breadcrumb-page"]');
	await expect(current).toBeVisible();
	await expect(current).toHaveText(TABLES[0]);
	// A middle segment is folded behind an ellipsis affordance rather than squeezing the trail…
	const ellipsis = crumbs.locator('[data-slot="breadcrumb-ellipsis"]');
	await expect(ellipsis).toBeVisible();
	// …and folding hides it from the ROW, not from the user: the skipped ancestor is still one click
	// away, so the trail stays navigable.
	await ellipsis.click();
	await expect(page.getByRole('menuitem', { name: 'Tables' })).toHaveAttribute(
		'href',
		'/catalog/tables',
	);
	await page.keyboard.press('Escape');
	// The breadcrumb bar itself never overflows its row.
	const fits = await crumbs.evaluate((el) => el.scrollWidth <= el.clientWidth + 1);
	expect(fits).toBe(true);
});

test('the breadcrumb is the path alone, and writes identifiers as they are', async ({ page }) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' }).locator('li');
	// innerText, not textContent: it is what the reader sees, CSS text-transform included, and an
	// identifier re-cased by a stylesheet misreports the data the page is about.
	const seen = { useInnerText: true } as const;

	// A catalog page: the trail is the path, with no project in front of it (a table belongs to the
	// project its warehouse names, which its own page says), and the table id is printed exactly as
	// the catalog names it.
	await page.goto(`/catalog/tables/${encodeURIComponent(TABLES[2])}`);
	await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
	await expect(crumbs).toHaveText(['Catalog', 'Tables', TABLES[2]], seen);
	// Separators go between crumbs: the trail starts with a name.
	await expect(crumbs.first().locator('svg')).toHaveCount(0);
	await expect(crumbs.nth(1).locator('svg')).toHaveCount(1);

	await page.goto('/settings/access');
	await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
	await expect(crumbs).toHaveText(['Settings', 'Users & roles'], seen);
	await page.goto('/projects');
	await expect(page.getByRole('button', { name: 'Account' })).toBeVisible();
	await expect(crumbs).toHaveText(['Projects'], seen);
});

test(`the sidebar is off-canvas below ${COLLAPSE_AT}px and on-canvas above it (#105)`, async ({
	page,
}) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page);
	// Desktop: the app nav is laid out in the document.
	await expect(page.locator('[data-slot="sidebar"][data-state="expanded"]')).toBeVisible();

	await page.setViewportSize({ width: 600, height: 900 });
	// Mobile: no in-document sidebar at all — it becomes a sheet behind the trigger.
	await expect(page.locator('[data-slot="sidebar"][data-state]')).toHaveCount(0);
	await page.getByRole('button', { name: 'Toggle Sidebar' }).click();
	await expect(page.locator('[data-sidebar="sidebar"][data-mobile="true"]')).toBeVisible();
});

/** Open the phone-width sheet from its trigger with the keyboard, on the tables list. */
async function openSheet(page: Page) {
	await page.setViewportSize({ width: 390, height: 844 });
	await openShell(page);
	const toggle = page.getByRole('button', { name: 'Toggle Sidebar' });
	await toggle.focus();
	await page.keyboard.press('Enter');
	const sheet = page.locator('[data-sidebar="sidebar"][data-mobile="true"]');
	await expect(sheet).toBeVisible();
	return sheet;
}

test('at phone width Escape on one of the sheet’s rows closes it', async ({ page }) => {
	// Each row is a tooltip trigger on the desktop rail; in the sheet none takes the key.
	const sheet = await openSheet(page);
	await sheet.getByRole('link', { name: 'Namespaces' }).focus();
	await page.keyboard.press('Escape');
	await expect(sheet).toHaveCount(0);
});

test('at phone width the sheet has a close button that closes it', async ({ page }) => {
	const sheet = await openSheet(page);
	const close = sheet.getByRole('button', { name: 'Close' });
	await expect(close).toBeVisible();
	await close.click();
	await expect(sheet).toHaveCount(0);
});

test('at phone width picking a page in the sheet goes there and closes it', async ({ page }) => {
	const sheet = await openSheet(page);
	await sheet.getByRole('link', { name: 'Warehouses' }).click();
	await expect(page).toHaveURL(/\/catalog\/warehouses$/);
	await expect(sheet).toHaveCount(0);
});

test('a closed group that holds the current page marks its heading', async ({ page }) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page);
	const nav = page.getByRole('navigation', { name: 'Pages' });
	const catalog = nav.getByRole('button', { name: /^Catalog/ });
	await catalog.click();
	await expect(nav.getByRole('link', { name: 'Tables', exact: true })).toHaveCount(0);
	await expect(catalog).toHaveAttribute('data-active', 'true');
	await expect(catalog).toHaveAccessibleName(/holds the current page/);
});

test('a table wider than the viewport scrolls INSIDE its card (#105)', async ({ page }) => {
	await page.setViewportSize({ width: 600, height: 900 });
	await openShell(page);
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
