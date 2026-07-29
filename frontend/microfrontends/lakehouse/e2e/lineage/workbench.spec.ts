import { test, expect, type Page, type Route } from '@playwright/test';

/**
 * The dock chrome, driven in a real browser.
 *
 * This file exists because the unit tests CANNOT cover what it covers. There is no DOM test
 * environment anywhere in this repo (no jsdom, happy-dom, testing-library or vitest-browser-svelte),
 * and dockview is a layout engine that measures elements in pixels — under jsdom every `offsetWidth`
 * is 0, so a "unit test" of a real `DockviewApi` would assert against a grid that believes it has no
 * size. Three things therefore have no proof at all until a browser runs them:
 *
 *   1. That the split compass and the panel picker are VISIBLE. Both render into the top layer via
 *      the native Popover API, specifically because `.dv-groupview { overflow: hidden }` clips an
 *      absolutely-positioned child, `.dv-animation`'s `will-change: transform` captures a fixed one,
 *      and `.dv-render-overlay { z-index: 1 }` paints panel bodies above group chrome. Every one of
 *      those failures is silent — the markup is correct and the element is simply not on screen.
 *   2. That adding a second panel of one component does not THROW. `addPanel` raises on a duplicate
 *      id, inside a click handler, where nothing catches it.
 *   3. That the workbench is reachable by CLICKING from the navbar. Every dock test before this one
 *      loaded the route directly, which is exactly why a missing navbar row went unnoticed.
 */

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

/** Open a navbar panel and hand back its viewport. Same retry rationale as `shell.spec.ts`: the
 *  triggers are server-rendered, so a click can land before bits-ui attaches its handlers. */
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
	await page.route('**/lakehouse/api/**', (route) => json(route, {}));
	await page.route('**/capi/**', (route) => {
		const path = new URL(route.request().url()).pathname;
		if (path.endsWith('/v1/me')) {
			return json(route, {
				sub: 'e2e',
				name: 'E2E',
				email: null,
				estate_admin: false,
				projects: [],
			});
		}
		return json(route, {});
	});
});

/** The dock mounts client-side after a dynamic import, so every assertion waits on a real tab. */
const gotoWorkbench = async (page: Page) => {
	await page.goto('/lakehouse/lineage/workbench');
	await expect(page.locator('.dv-tab').first()).toBeVisible({ timeout: 30_000 });
};

test('the workbench is reachable by CLICKING from the navbar, not only by URL', async ({
	page,
}) => {
	// Item 6's browser half. Loading the route directly is what let a missing navbar row ship: every
	// earlier dock test did exactly that and passed while the feature was unreachable from any other
	// zone. So this navigates the way a person does.
	await page.goto('/lakehouse/lineage');
	const panel = await openPanel(page, 'Lakehouse');
	// Located by HREF, not by an exact accessible name: a panel row renders its title AND its
	// one-line description, so the row's accessible name is "Workbench Graph, runs and events in one
	// arrangeable dock." The href is the thing that actually has to be right anyway.
	const link = panel.locator('a[href="/lakehouse/lineage/workbench"]');
	await expect(link).toBeVisible();
	await expect(link).toContainText('Workbench');
	await link.click();
	await expect(page).toHaveURL(/\/lakehouse\/lineage\/workbench$/);
	await expect(page.locator('.dv-tab').first()).toBeVisible({ timeout: 30_000 });
});

test('the dock seeds its three panels', async ({ page }) => {
	await gotoWorkbench(page);
	for (const name of ['Graph', 'Runs', 'Events']) {
		await expect(page.locator('.dv-tab', { hasText: name }).first()).toBeVisible();
	}
});

test('the split compass offers all FOUR directions from the header', async ({ page }) => {
	// Item 1's browser half. The header shipped two directions (right, down); up and left existed only
	// as tab context-menu rows. This asserts the pad opens AND carries four — and it is a real
	// visibility check, which is the whole reason the pad uses the top layer.
	await gotoWorkbench(page);
	await page.getByRole('button', { name: 'Split this pane' }).first().click();

	const pad = page.getByRole('group', { name: 'Split direction' });
	await expect(pad).toBeVisible();
	for (const word of ['up', 'down', 'left', 'right']) {
		await expect(pad.getByRole('button', { name: new RegExp(`${word}$`, 'i') })).toBeVisible();
	}
	// Escape is UA light-dismiss, not code we wrote — asserted because we rely on it instead of
	// shipping a focus trap.
	await page.keyboard.press('Escape');
	await expect(pad).toBeHidden();
});

test('splitting UP actually creates a second group', async ({ page }) => {
	await gotoWorkbench(page);
	const before = await page.locator('.dv-groupview').count();
	await page.getByRole('button', { name: 'Split this pane' }).first().click();
	await page
		.getByRole('group', { name: 'Split direction' })
		.getByRole('button', { name: /up$/i })
		.click();
	await expect(page.locator('.dv-groupview')).toHaveCount(before + 1);
});

test('the "+" picker searches, and adding a second copy does not throw', async ({ page }) => {
	// Item 2's browser half, and the one assertion that cannot be faked in node: `addPanel` throws on
	// a duplicate id, inside a click handler where nothing catches it. `uniquePanelId` is what stops
	// that, and only a browser can show the click succeeding.
	const errors: string[] = [];
	page.on('pageerror', (e) => errors.push(e.message));

	await gotoWorkbench(page);
	const runsTabs = page.locator('.dv-tab', { hasText: 'Runs' });
	await expect(runsTabs).toHaveCount(1);

	await page.getByRole('button', { name: 'Add a panel' }).first().click();
	const picker = page.getByRole('group', { name: 'Add a panel' });
	await expect(picker).toBeVisible();

	// Search over label + key + keywords. "executions" is a KEYWORD of Runs, not its label — so a hit
	// proves the keyword field is actually consulted.
	await picker.getByRole('searchbox', { name: 'Search panels' }).fill('executions');
	await expect(picker.getByRole('button', { name: /Runs/ })).toBeVisible();
	await expect(picker.getByRole('button', { name: /Graph/ })).toBeHidden();

	await picker.getByRole('button', { name: /Runs/ }).click();
	await expect(runsTabs).toHaveCount(2);
	expect(errors, 'adding a second panel of one component threw').toEqual([]);
});

test('the picker reports an empty result rather than an empty box', async ({ page }) => {
	await gotoWorkbench(page);
	await page.getByRole('button', { name: 'Add a panel' }).first().click();
	const picker = page.getByRole('group', { name: 'Add a panel' });
	await picker.getByRole('searchbox', { name: 'Search panels' }).fill('zzzznomatch');
	await expect(picker.getByText(/No panel matches/)).toBeVisible();
});

test('split and "+" are SEPARATE controls, both present', async ({ page }) => {
	// The owner's specification, asserted where it can actually be observed. An earlier draft of the
	// backlog had add-panel REPLACING split.
	await gotoWorkbench(page);
	await expect(page.getByRole('button', { name: 'Split this pane' }).first()).toBeVisible();
	await expect(page.getByRole('button', { name: 'Add a panel' }).first()).toBeVisible();
});
