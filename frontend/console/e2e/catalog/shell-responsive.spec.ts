import { test, expect, type Route, type Page } from '@playwright/test';
import { AUTH_OFF } from '../ports';
import { MOCK_CATALOG } from '../admin/ports';

// Responsive contract for the SHARED @rask/ui shell (#105). It lives in the console because a
// spec needs *some* page to render the shell in, but it imports nothing page-specific and asserts
// nothing about the console's own content — every assertion is about chrome that every
// app inherits from `AppShell`:
//
//   1. the document never scrolls sideways (scrollWidth <= clientWidth) at any width, so a wide
//      table scrolls INSIDE its container and page content is never sliced off the viewport;
//   2. the account control never overlaps a navbar entry — below the shell's collapse breakpoint
//      the app links fold into a single overflow menu instead of running under the avatar.
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

/** The shell's collapse breakpoint (`SHELL_COLLAPSE_BREAKPOINT`) — BELOW it the app links live in
 *  one overflow menu and the sidebar is an off-canvas sheet; at or above it, the wide bar. */
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

/**
 * Drive the shell on a PROJECT host (`acmecorp.localhost`), not the bare dev host. The estate's IA
 * is project-first — the project IS the request host — so the navbar's project switcher renders a
 * real label there instead of the "Default" placeholder, which is what made the row wide enough for
 * the last page trigger to slide under the account avatar in the first place. Asserting on the bare
 * host would measure a narrower bar than any real deployment ever shows.
 */
function projectUrl(baseURL: string | undefined, path: string): string {
	const url = new URL(path, baseURL ?? AUTH_OFF);
	url.hostname = `acmecorp.${url.hostname}`;
	return url.href;
}

/** Land on a page route with the shell fully resolved: the identity has landed (so the navbar is
 *  past its skeleton state) and the table has rendered. */
async function openShell(
	page: Page,
	baseURL: string | undefined,
	path = '/catalog/tables',
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
		// #105 IS FIXED, and this is what fixed it — not a layout change. The marker here was
		// `test.fail(width === 1024, …)`: with EIGHT entries the wide bar genuinely collided with the
		// account control at exactly 1024px, the collapse boundary. The two-level ruling took Projects
		// and Settings out of the in-project bar and another session retired the workbench page, so the
		// bar is six entries and no longer overflows. The marker then failed as "expected to fail, but
		// passed", which is precisely why it was written that way — a known defect that goes quiet on
		// its own is a defect nobody removes. Deleted with its cause, and the geometry below now runs
		// for real at every width.
		await page.setViewportSize({ width, height: 900 });
		await openShell(page, baseURL);

		const account = await page.getByRole('button', { name: 'Account' }).boundingBox();
		expect(account).not.toBeNull();

		// Every VISIBLE navbar entry: the app triggers/links on the desktop bar, or the single
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

test('below the breakpoint the app links collapse into one overflow menu that still reaches every destination (#105)', async ({
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

	// Every page, and the sub-areas underneath them, stay reachable from the one menu — one group per
	// page (the icon contributes a leading space to the accessible text). This list is the SHELL's
	// truth: a page missing here is the R15 defect (a page absent from the navbar), not a stale test.
	// Projects LEADS it and Settings CLOSES it — the two groups that are not pages, both from the IA
	// This page is inside a project, so the collapsed bar folds the IN-PROJECT bar — the Lakehouse entry
	// the wide bar carries. Home, Projects and Settings are the estate level and are not in this bar at
	// either width; pinning the exact list catches collapsing smuggling them back in.
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(panel.locator('[data-slot="navbar-overflow-group"]')).toHaveText([' Lakehouse']);
	// One row per destination the wide bar reaches, the estate-admin-only operations rows included.
	for (const href of [
		// NO '/projects' HERE: the estate's project list is a MAIN-MENU destination.
		'/catalog',
		'/catalog/tables',
		'/catalog/warehouses',
		'/workbench',
		'/admin/dlq',
	]) {
		await expect(panel.locator(`a[href="${href}"]`)).toHaveCount(1);
	}
	// The panel itself stays inside the viewport — an overflow menu that hangs off the edge is the
	// bug wearing a different hat.
	const box = await panel.boundingBox();
	expect(box!.x).toBeGreaterThanOrEqual(0);
	expect(box!.x + box!.width).toBeLessThanOrEqual(600);
});

test('above the breakpoint the app links stay on the bar (no overflow menu) (#105)', async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page, baseURL);
	await expect(page.getByRole('button', { name: 'Menu' })).toHaveCount(0);
	await expect(page.getByRole('button', { name: 'Lakehouse' })).toBeVisible();
});

test('the wide Lakehouse panel carries NO filler self-row — its sub-area rows are the way in', async ({
	page,
	baseURL,
}) => {
	// INVERTED BY RULING (2026-08-03, f5dd1f0 — pinned in top-navbar.svelte's `groups` branch). This
	// test used to demand the opposite: a synthesized "Lakehouse · Open the console." row inside
	// the wide panel. The ruling deleted it, and the reasoning is what this assertion now guards:
	//   - the row linked /catalog, a redirect-to-tables page, so it said nothing a real row
	//     (Tables, Namespaces, Warehouses, …) does not already say;
	//   - it rendered for THIS page alone — every flat-`items` page carries a real, function-titled root
	//     row (Search at /explorer/, Overview at /compute/), so their equivalent is skipped as a duplicate;
	//   - the app root is NOT unreachable: the narrow overflow menu still prepends every page's root
	//     uniformly, which the "below the breakpoint …" test above pins by asserting /catalog.
	await page.setViewportSize({ width: 1440, height: 900 });
	await openShell(page, baseURL);
	await page.getByRole('button', { name: 'Lakehouse' }).click();
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(panel.locator('a[href="/catalog"]')).toHaveCount(0);
	// The trigger stays a BUTTON either way — this ruling removed a row, it did not turn the trigger
	// into a link (which would navigate instead of opening the panel and hide every column behind a
	// page load).
	await expect(page.getByRole('button', { name: 'Lakehouse' })).toBeVisible();
	const nav = page.getByRole('navigation', { name: 'Zones' });
	await expect(nav.getByRole('link', { name: 'Lakehouse', exact: true })).toHaveCount(0);
	// What a user clicks instead: the real rows, one per area, each resolving to a page that exists.
	for (const href of ['/catalog/tables', '/catalog/namespaces', '/catalog/warehouses']) {
		await expect(panel.locator(`a[href="${href}"]`)).toHaveCount(1);
	}
	// The tenants list is gone from this panel too, by the SAME ruling: there is ONE project concept
	// and it is the TOP of the hierarchy (project > warehouse > namespace > table), so a tenants row
	// inside a project-scoped page's dropdown inverted it. The IA round then deleted the route
	// outright — the surface is the console's `/projects` — so this href resolves nowhere; the
	// assertion stays as the guard against the dead row returning.
	await expect(panel.locator('a[href="/catalog/projects"]')).toHaveCount(0);

	// …and the panel must still RENDER its columns. Kept from the version this test replaced, because
	// it is the assertion that earned its keep: when the self-row briefly existed it was a sibling
	// before the grid, giving NavigationMenu.Content two children — bits-ui sizes the shared viewport
	// from the active content, measured 69px (the row's own height) and clipped all five columns. Every
	// href assertion above passed anyway (the links were in the DOM and Playwright called them visible;
	// the clip is on an ancestor). Only a screenshot caught it. So assert the rendered GEOMETRY too —
	// now guarding the grid-only panel against the same class of collapse.
	const box = await panel.boundingBox();
	expect(box!.height, 'the panel is clipped — the group columns are not showing').toBeGreaterThan(
		200,
	);
	// And assert on innerText, not textContent: textContent returns every panel's markup including the
	// inactive siblings', which is how "alice sees the Governance column" passed against a 69px panel.
	// innerText reflects RENDERED text, and the group labels are CSS-uppercased, so compare case-insensitively.
	const visible = (await panel.innerText()).toLowerCase();
	for (const group of ['workspace', 'catalog']) {
		expect(visible, `the ${group} column is not visible in the panel`).toContain(group);
	}
});

test('the breadcrumb truncates MIDDLE segments and always shows the current page (#105)', async ({
	page,
	baseURL,
}) => {
	await page.setViewportSize({ width: 600, height: 900 });
	await page.goto(projectUrl(baseURL, `/catalog/tables/${encodeURIComponent(TABLES[0])}`));
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
	// Desktop: the app nav is laid out in the document.
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
