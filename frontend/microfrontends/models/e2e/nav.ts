import { expect, type Page } from '@playwright/test';

/**
 * Open a navbar panel and hand back its viewport.
 *
 * The triggers are server-rendered, so on a loaded machine a click can land before bits-ui has attached
 * its handlers: the markup is inert rather than broken and the panel silently never opens. Retrying the
 * click rides out that race — what must hold (the panel DOES open, and carries the rows the caller
 * asserts) is unchanged; only the delivery is made robust.
 *
 * It clicks only while the panel is CLOSED, so a retry can never toggle an already-open panel back shut.
 * Callers switching between panels close the current one first (Escape) rather than clicking through, for
 * the same reason.
 */
export async function openPanel(page: Page, name: string) {
	const trigger = page
		.getByRole('navigation', { name: 'Zones' })
		.getByRole('button', { name, exact: true });
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(async () => {
		if (!(await panel.isVisible())) await trigger.click();
		await expect(panel).toBeVisible({ timeout: 1_000 });
	}).toPass({ timeout: 20_000 });
	return panel;
}

/** Close whatever panel is open, and wait until it is really gone. */
export async function closePanel(page: Page) {
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(async () => {
		if (await panel.isVisible()) await page.keyboard.press('Escape');
		await expect(panel).toBeHidden({ timeout: 1_000 });
	}).toPass({ timeout: 20_000 });
}
