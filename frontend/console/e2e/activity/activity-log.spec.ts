import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed } from '../mock-client';

// THE BELL lists the reader's own writes and their outcomes, kept in the browser for this tab. The
// write is a project delete, refused once (the project is protected) and then forced, so one write the
// catalog refuses and one it accepts land in the log, through the real sign-in round trip and the real
// write path to the mock catalog.

/** The catalog's refusal of an unforced delete of a protected project (`fga_deps.require_not_protected`). */
const PROTECTED_DETAIL =
	"project 'acme' is protected against deletion. Pass force=true to override (protection only — the " +
	'same authorization is still required), or clear the flag first.';

const bell = (page: Page) => page.getByRole('button', { name: /^Activity/ });

test('the bell logs each write with its outcome, keeps it across a reload, and forgets it at sign-out', async ({
	context,
	page,
}, testInfo) => {
	const token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, token, {
		'GET /management/v1/me': ME_ADMIN,
		'GET /management/v1/projects/acme': { project: 'acme', warehouses: [], admins: ['user:alice'] },
		'DELETE /management/v1/projects/acme': {
			status: 409,
			body: { title: 'NamespaceNotEmptyError', status: 409, detail: PROTECTED_DETAIL },
		},
		'DELETE /management/v1/projects/acme?force=true': { project: 'acme', tuples_revoked: 1 },
		'GET /management/v1/projects': [],
	});

	await page.goto('/projects/acme');
	await expect(bell(page)).toHaveAccessibleName('Activity');
	await page.getByRole('button', { name: 'Delete project' }).click();
	const dialog = page.getByRole('alertdialog');
	await dialog.getByRole('button', { name: 'Delete acme' }).click();
	await expect(dialog).toContainText('is protected against deletion');
	await dialog.getByLabel('Override deletion protection').check();
	await dialog.getByRole('button', { name: 'Force delete acme' }).click();
	await expect(page).toHaveURL(/\/projects$/);

	// Both writes count as new until the panel is opened.
	await expect(bell(page)).toHaveAccessibleName('Activity, 2 new');

	// A reload starts the sign-in round trip again; the same person's log comes back with it.
	await page.reload();
	await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
	await expect(bell(page)).toHaveAccessibleName('Activity, 2 new');

	await bell(page).click();
	const panel = page.getByRole('dialog', { name: 'Activity' });
	const entries = panel.locator('[data-slot="activity-entry"]');
	await expect(entries).toHaveCount(2);
	// Newest first: the forced delete the catalog accepted, then the refusal with its status and detail.
	await expect(entries.nth(0)).toContainText('Force-delete project acme');
	await expect(entries.nth(0)).toContainText('Succeeded');
	// The bell prints a moment as the event feed does: the full local date and time, and its zone.
	await expect(entries.nth(0).locator('time')).toHaveText(
		/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC([+-]\d{2}:\d{2})?$/,
	);
	await expect(entries.nth(1)).toContainText('Delete project acme');
	await expect(entries.nth(1)).toContainText('Failed (HTTP 409)');
	await expect(entries.nth(1)).toContainText(PROTECTED_DETAIL);
	await page.keyboard.press('Escape');
	// Opening the panel saw them.
	await expect(bell(page)).toHaveAccessibleName('Activity');

	await page.getByRole('button', { name: 'Account' }).click();
	await page.getByRole('menuitem', { name: 'Sign out' }).click();
	await expect(page.getByRole('heading', { name: 'Mock IdP signed out' })).toBeVisible();

	// Signed in again in the same tab, the log is empty: nothing of the session outlived it.
	await page.goto('/projects');
	await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
	await bell(page).click();
	await expect(page.getByRole('dialog', { name: 'Activity' })).toContainText('Nothing yet');
});
