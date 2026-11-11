import { expect, test, type Page, type Route } from '@playwright/test';

// The /workbench dock on the auth-off origin. Its two reads are answered in the browser (`page.route`)
// rather than by seeding the shared mock catalog, whose auth-off bucket another spec seeds.

const TABLES = ['db1$t', 'db1$u'];
const EVENTS = {
	events: [
		{ seq: 1, event_type: 'COMPLETE', job: 'bronze.ingest', event_time: '2026-11-11T10:00:00Z' },
	],
};

const json = (route: Route, body: unknown) =>
	route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

/** Answer the registry and the event feed; `hold` stops answering both, so what a panel shows after
 *  that comes from what was read before. */
async function answerReads(page: Page): Promise<{ hold: () => void }> {
	let held = false;
	const answer = (body: unknown) => (route: Route) =>
		held ? new Promise<void>(() => {}) : json(route, body);
	await page.route('**/api/catalog/v1/table', answer({ tables: TABLES }));
	await page.route('**/api/lineage/events?*', answer(EVENTS));
	return { hold: () => (held = true) };
}

const tab = (page: Page, title: string) => page.getByRole('tab', { name: new RegExp(`^${title}`) });

async function open(page: Page): Promise<{ hold: () => void }> {
	const reads = await answerReads(page);
	await page.goto('/workbench');
	await expect(page.getByRole('link', { name: TABLES[0] })).toBeVisible();
	await expect(page.getByText('bronze.ingest')).toBeVisible();
	return reads;
}

test('both panels render their data, side by side', async ({ page }) => {
	await open(page);
	await expect(page.getByRole('tablist')).toHaveCount(2);
	await expect(tab(page, 'Tables')).toBeVisible();
	await expect(tab(page, 'Events')).toBeVisible();
});

test('a closed panel is offered by the panel picker and opens again', async ({ page }) => {
	await open(page);
	await page.getByRole('button', { name: 'Close Events' }).click();
	await expect(tab(page, 'Events')).toHaveCount(0);
	await expect(page.getByText('bronze.ingest')).toHaveCount(0);

	await page.getByRole('button', { name: 'Open a panel' }).click();
	await page.getByRole('menuitem', { name: 'Events' }).click();
	await expect(tab(page, 'Events')).toBeVisible();
	await expect(page.getByText('bronze.ingest')).toBeVisible();
});

test('a panel maximizes over the dock and restores to where it was', async ({ page }) => {
	await open(page);
	await page.getByRole('button', { name: 'Maximize Tables' }).click();
	await expect(page.getByRole('tablist')).toHaveCount(0);
	await expect(page.getByRole('link', { name: TABLES[0] })).toBeVisible();
	await expect(page.getByText('bronze.ingest')).toHaveCount(0);

	await page.getByRole('button', { name: 'Restore' }).click();
	await expect(page.getByRole('tablist')).toHaveCount(2);
	await expect(page.getByText('bronze.ingest')).toBeVisible();
});

test('a popped-out panel opens styled in its own window, and comes back when it closes', async ({
	page,
	context,
}) => {
	// No routed reads here: with a route on the context, Playwright holds the stylesheet requests of
	// a window opened as `about:blank`, so the window would look unstyled for a reason the console
	// does not have. The Events panel renders whatever the mock lineage feed answers.
	const refused: string[] = [];
	const watch = (m: { text: () => string }) => {
		if (/Content Security Policy/i.test(m.text())) refused.push(m.text());
	};
	page.on('console', watch);
	await page.goto('/workbench');
	await expect(tab(page, 'Events')).toBeVisible();

	const [popout] = await Promise.all([
		context.waitForEvent('page'),
		page.getByRole('button', { name: 'Pop out Events' }).click(),
	]);
	popout.on('console', watch);
	await expect(popout).toHaveTitle('Events');
	await expect(tab(page, 'Events')).toHaveCount(0);
	// Styled: the page's sheets reached the window: the library's popout frame lays out as a column,
	// and the panel's Tailwind utilities apply (`text-xs`), rather than the browser's defaults.
	const frame = popout.locator('.horizon-layout__popout');
	await expect(frame).toHaveCSS('display', 'flex');
	await expect(frame).toHaveCSS('flex-direction', 'column');
	await expect(frame.locator('.text-xs').first()).toHaveCSS('font-size', '12px');

	await popout.close();
	await expect(tab(page, 'Events')).toBeVisible();
	expect(refused).toEqual([]);
});

test('popped-out windows stay open when the dock is left empty, and their panels come back', async ({
	page,
	context,
}) => {
	// No routed reads, for the reason the test above gives.
	await page.goto('/workbench');
	await expect(tab(page, 'Events')).toBeVisible();
	const popOut = async (title: string) => {
		const [window] = await Promise.all([
			context.waitForEvent('page'),
			page.getByRole('button', { name: `Pop out ${title}` }).click(),
		]);
		await expect(window).toHaveTitle(title);
		return window;
	};
	const events = await popOut('Events');
	const tables = await popOut('Tables');
	await expect(page.getByText('Every open panel is in its own window.')).toBeVisible();
	expect(events.isClosed()).toBe(false);
	expect(tables.isClosed()).toBe(false);

	await events.close();
	await tables.close();
	await expect(tab(page, 'Events')).toBeVisible();
	await expect(tab(page, 'Tables')).toBeVisible();
});

test('closing the last docked panel leaves a popped-out window open', async ({ page, context }) => {
	await page.goto('/workbench');
	await expect(tab(page, 'Events')).toBeVisible();
	const [events] = await Promise.all([
		context.waitForEvent('page'),
		page.getByRole('button', { name: 'Pop out Events' }).click(),
	]);
	await expect(events).toHaveTitle('Events');
	await page.getByRole('button', { name: 'Close Tables' }).click();
	await expect(tab(page, 'Tables')).toHaveCount(0);
	expect(events.isClosed()).toBe(false);

	await events.close();
	await expect(tab(page, 'Events')).toBeVisible();
});

test('the keyboard alone moves a tab between groups, splits it out again and resizes the split', async ({
	page,
}) => {
	await open(page);
	const lists = page.getByRole('tablist');
	const holdingEvents = lists.filter({ has: tab(page, 'Events') });

	// Ctrl+Left moves the Events tab into the group on its left: one group of two tabs. The group the
	// keyboard was in is gone, so the keyboard goes with the tab, and the tab is the selected one.
	await lists.nth(1).focus();
	await page.keyboard.press('Control+ArrowLeft');
	await expect(lists).toHaveCount(1);
	await expect(lists.first().getByRole('tab')).toHaveCount(2);
	await expect(holdingEvents).toBeFocused();
	await expect(page.getByRole('tab', { name: /^Events/, selected: true })).toBeVisible();
	await expect(page.getByRole('tabpanel', { name: /^Events/ })).toContainText('bronze.ingest');

	// Alt+Right splits the active tab (Events, the one just moved in) back out into a pane of its own,
	// pressed where the keyboard already is.
	await page.keyboard.press('Alt+ArrowRight');
	await expect(lists).toHaveCount(2);
	await expect(lists.nth(1).getByRole('tab')).toHaveText('Events');
	await expect(holdingEvents).toBeFocused();

	// The resizer is a slider: an arrow key moves it a step.
	const resizer = page.getByRole('slider', { name: 'Resize pane 1' });
	await expect(resizer).toHaveAttribute('aria-valuenow', '50');
	await resizer.focus();
	await page.keyboard.press('ArrowRight');
	await page.keyboard.press('ArrowRight');
	await expect(resizer).toHaveAttribute('aria-valuenow', '52');
});

test("a panel's data is there again after its tab was switched away and back", async ({ page }) => {
	const reads = await open(page);
	await page.getByRole('button', { name: 'Close Events' }).click();
	await page.getByRole('button', { name: 'Open a panel' }).click();
	await page.getByRole('menuitem', { name: 'Events' }).click();

	// From here no read is answered: what the panels show comes from what they read before. The wait
	// outlasts the 1 s in which a read settled is reused (`reads.svelte.ts`), so a remounted panel
	// that read again would get no answer.
	reads.hold();
	await page.waitForTimeout(1_100);
	await tab(page, 'Tables').click();
	await expect(page.getByRole('link', { name: TABLES[0] })).toBeVisible();
	await tab(page, 'Events').click();
	await expect(page.getByText('bronze.ingest')).toBeVisible();
	await tab(page, 'Tables').click();
	await expect(page.getByRole('link', { name: TABLES[1] })).toBeVisible();
});
