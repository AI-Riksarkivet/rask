import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from './session';
import { calls, seed as seedFor } from './mock-client';

// Retiring a tenant — `DELETE /v1/projects/{id}` from `/projects/<p>`'s danger zone.
//
// The read and the write both run on the ZONE SERVER (a remote `query` and a remote `command`), so the
// catalog is stubbed upstream and seeded per bearer rather than `page.route`d — the same reason every
// other spec in this directory drives the mock.
//
// What is worth pinning here is the REFUSAL, not the happy path. The delete door has no `cascade` at
// all: a project holding warehouses answers 409 and its RFC 9457 `detail` NAMES them, and the user's
// next action is to go delete those warehouses. So the names have to survive the trip from a prose
// error message onto the screen AS LINKS — cross-zone ones, carrying `data-sveltekit-reload`, because
// warehouses live in the lakehouse and a soft nav would resolve against home's route manifest and 404
// the user out of the very fix they were handed. `@rask/zone-contract`'s cross-zone-reload suite gates
// the ATTRIBUTE on those anchors (it globs `microfrontends/*/src/**/*.svelte`, home included, and
// passes the owning zone) — what it cannot see is that the hrefs exist at all, which is a runtime
// product of parsing prose out of a 409 and is what the first test below pins.

/** The catalog's real 409 body when a project still holds warehouses (`endpoints/projects.py`) — the
 *  message is copied verbatim, because parsing the blocker names out of it is the thing under test. */
const HELD_BY_WAREHOUSES = {
	status: 409,
	body: {
		type: 'https://lance.org/problems/namespacenotemptyerror',
		title: 'NamespaceNotEmptyError',
		status: 409,
		detail:
			"project 'acme' still holds 2 warehouse(s): acme-gold, acme-wh. Delete each one first — " +
			'DELETE /v1/warehouses/{id} (its own bucket purge is a separate opt-in) — then delete the ' +
			'project. There is deliberately no cascade here: retiring a tenant must never be able to ' +
			'destroy its buckets transitively in one request.',
	},
};

/** The OTHER 409 (`fga_deps.require_not_protected`) — the one `force` is for. */
const PROTECTED = {
	status: 409,
	body: {
		type: 'https://lance.org/problems/namespacenotemptyerror',
		title: 'NamespaceNotEmptyError',
		status: 409,
		detail:
			"project 'acme' is protected against deletion. Pass force=true to override (protection only " +
			'— the same authorization is still required), or clear the flag first.',
	},
};

/** A project with nothing left in it — the only shape whose delete can succeed. */
const EMPTY_PROJECT = { project: 'acme', warehouses: [], admins: ['user:alice'] };

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

/** Every DELETE this test's bearer put on the wire, PATH INCLUDING QUERY — `?force=true` is the whole
 *  point of the force test, and the mock records the search string with the path. */
const deletePaths = async (page: Page): Promise<string[]> =>
	(await calls(page, token)).filter((c) => c.method === 'DELETE').map((c) => c.path);

/** Open the danger zone's confirm dialog on one project's page. */
async function openDeleteDialog(page: Page, project: string) {
	await page.goto(`/projects/${project}`);
	await page.getByRole('button', { name: 'Delete project' }).click();
	const dialog = page.getByRole('alertdialog');
	await expect(dialog).toBeVisible();
	return dialog;
}

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, { 'GET /v1/me': ME_ADMIN });
});

test('a 409 names the blocking warehouses and renders each as a cross-zone link', async ({
	page,
}) => {
	await seed(page, {
		'GET /v1/projects/acme': {
			project: 'acme',
			warehouses: [
				{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' },
				{ id: 'acme-gold', bucket: 'gold-bucket', serving: 'gold', status: 'active' },
			],
			admins: ['user:alice'],
		},
		'DELETE /v1/projects/acme': HELD_BY_WAREHOUSES,
	});
	const dialog = await openDeleteDialog(page, 'acme');
	await dialog.getByRole('button', { name: 'Delete acme' }).click();

	// Every warehouse the refusal NAMED, as a link into the lakehouse — one click from "you can't
	// delete this" to the door that unblocks it.
	for (const id of ['acme-gold', 'acme-wh']) {
		const link = dialog.locator(`a[href="/lakehouse/catalog/warehouses/${id}"]`);
		await expect(link).toBeVisible();
		// Without this a soft nav resolves against home's manifest, which owns no /lakehouse route → 404.
		await expect(link).toHaveAttribute('data-sveltekit-reload', '');
	}
	// The upstream message is surfaced VERBATIM alongside them: it says WHY there is no cascade, which
	// no paraphrase of ours would.
	await expect(dialog).toContainText('There is deliberately no cascade here');
	// The dialog STAYS OPEN — a confirm that closed on the 409 would throw the blocker list away with it
	// — and re-confirming is refused, because nothing changed on this screen that could change the answer.
	await expect(dialog.getByRole('button', { name: 'Delete acme' })).toBeDisabled();
	// …and `force` is NOT on offer: it turns the `protected` flag, which this refusal did not name.
	// Offering it on any 409 would advertise an override for a lock the server never mentioned.
	await expect(dialog.getByLabel('Override deletion protection')).toHaveCount(0);
	// A blocked delete is not a partial one: exactly one request went out and it removed nothing.
	expect(await deletePaths(page)).toEqual(['/v1/projects/acme']);
});

test('a successful delete toasts the response’s tuple count and lands back on the gallery', async ({
	page,
}) => {
	await seed(page, {
		'GET /v1/projects/acme': EMPTY_PROJECT,
		'DELETE /v1/projects/acme': { project: 'acme', tuples_revoked: 4 },
		// The estate AFTER the retirement — what the navigation's fresh load reads.
		'GET /v1/projects': [{ project: 'beta', warehouses: [] }],
	});
	const dialog = await openDeleteDialog(page, 'acme');
	await dialog.getByRole('button', { name: 'Delete acme' }).click();

	// The count comes from the RESPONSE, not from anything the browser knew before it — `tuples_revoked`
	// is the one fact only the catalog holds, and it is the evidence the authz side really happened.
	await expect(page.getByText('Project acme retired — 4 tuples revoked.')).toBeVisible();
	// …and the retired project's own page is not somewhere to stay: the next read of it would 404.
	await expect(page).toHaveURL(/\/projects$/);
	await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
	await expect(page.locator('a[href="/projects/acme"]')).toHaveCount(0);
});

test('force is offered ONLY for a protected project, and rides the wire as ?force=true', async ({
	page,
}) => {
	// The with-query seed is looked up before the bare one, so the FIRST attempt 409s and the forced
	// retry succeeds — the two-step the user actually walks.
	await seed(page, {
		'GET /v1/projects/acme': EMPTY_PROJECT,
		'DELETE /v1/projects/acme': PROTECTED,
		'DELETE /v1/projects/acme?force=true': { project: 'acme', tuples_revoked: 1 },
	});
	const dialog = await openDeleteDialog(page, 'acme');
	// Nothing has refused yet, so the override is not on offer — it is not a general-purpose switch.
	await expect(dialog.getByLabel('Override deletion protection')).toHaveCount(0);

	await dialog.getByRole('button', { name: 'Delete acme' }).click();
	await expect(dialog).toContainText('is protected against deletion');
	// It appears now, and it is labelled for what it actually overrides. Forcing is NOT an authz bypass:
	// the project-admin gate ran first and runs identically either way.
	const override = dialog.getByLabel('Override deletion protection');
	await expect(override).toBeVisible();
	await expect(dialog).toContainText('never authorization');
	await override.check();

	await dialog.getByRole('button', { name: 'Force delete acme' }).click();
	await expect(page.getByText('Project acme retired — 1 tuple revoked.')).toBeVisible();
	// The wire is the assertion: an unchecked box sends no `force` at all (never `force=false`), and the
	// checked one sends it as a query param on the same path.
	expect(await deletePaths(page)).toEqual(['/v1/projects/acme', '/v1/projects/acme?force=true']);
});

test('a refusal does not outlive the dialog it was made in', async ({ page }) => {
	// The blocker case is the one that LOCKS: while blockers stand the confirm is disabled, so a stale
	// list is not merely wrong copy — nothing on the reopened screen can ever re-enable the button. The
	// user's whole path is "be refused → go delete those warehouses → come back", which is exactly the
	// trip this asserts.
	await seed(page, {
		'GET /v1/projects/acme': {
			project: 'acme',
			warehouses: [{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' }],
			admins: ['user:alice'],
		},
		'DELETE /v1/projects/acme': HELD_BY_WAREHOUSES,
	});
	const dialog = await openDeleteDialog(page, 'acme');
	await dialog.getByRole('button', { name: 'Delete acme' }).click();
	await expect(dialog.locator('a[href="/lakehouse/catalog/warehouses/acme-wh"]')).toBeVisible();

	await dialog.getByRole('button', { name: 'Close' }).click();
	await expect(dialog).toBeHidden();

	await page.getByRole('button', { name: 'Delete project' }).click();
	const reopened = page.getByRole('alertdialog');
	await expect(reopened).toBeVisible();
	await expect(reopened.locator('a[href^="/lakehouse/catalog/warehouses/"]')).toHaveCount(0);
	await expect(reopened.getByRole('button', { name: 'Delete acme' })).toBeEnabled();
});

test('a force opt-in does not survive the dialog it was made in', async ({ page }) => {
	await seed(page, {
		'GET /v1/projects/acme': EMPTY_PROJECT,
		'DELETE /v1/projects/acme': PROTECTED,
		'DELETE /v1/projects/acme?force=true': { project: 'acme', tuples_revoked: 1 },
	});
	const dialog = await openDeleteDialog(page, 'acme');
	await dialog.getByRole('button', { name: 'Delete acme' }).click();
	await expect(dialog).toContainText('is protected against deletion');
	await dialog.getByLabel('Override deletion protection').check();

	await dialog.getByRole('button', { name: 'Cancel' }).click();
	await expect(dialog).toBeHidden();

	// An override armed in a dialog the user then CANCELLED must not still be armed. The catalog has
	// refused nothing this time round, so the lock it turns is not even known to be on.
	await page.getByRole('button', { name: 'Delete project' }).click();
	const reopened = page.getByRole('alertdialog');
	await expect(reopened).toBeVisible();
	await expect(reopened.getByLabel('Override deletion protection')).toHaveCount(0);
	await expect(reopened.getByRole('button', { name: 'Force delete acme' })).toHaveCount(0);

	// The wire is the proof: the second attempt is a PLAIN delete, not a forced one.
	await reopened.getByRole('button', { name: 'Delete acme' }).click();
	await expect(reopened).toContainText('is protected against deletion');
	expect(await deletePaths(page)).toEqual(['/v1/projects/acme', '/v1/projects/acme']);
});

test('a 404 is rendered as the no-oracle refusal it is, not as "deleted"', async ({ page }) => {
	// The delete door answers 404 for BOTH "no such project" and "not yours" on purpose — otherwise any
	// authenticated caller could enumerate the estate's tenants by the 404-vs-403 difference. The UI must
	// not invent the distinction the API refuses to make, and must not read as a success either.
	await seed(page, {
		'GET /v1/projects/acme': EMPTY_PROJECT,
		'DELETE /v1/projects/acme': { status: 404, body: { detail: 'project not found: acme' } },
	});
	const dialog = await openDeleteDialog(page, 'acme');
	await dialog.getByRole('button', { name: 'Delete acme' }).click();

	await expect(dialog).toContainText('either no longer exists or you do not administer it');
	await expect(dialog).toContainText('project not found: acme');
	// Still on the project, still open, nothing toasted as retired.
	await expect(page).toHaveURL(/\/projects\/acme$/);
	await expect(page.getByText(/retired —/)).toHaveCount(0);
});
