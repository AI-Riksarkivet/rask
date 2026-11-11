import { expect, test, type Page } from '@playwright/test';
import { mockMe, signIn, TOKEN } from './session';
import { MOCK_CATALOG } from './ports';

// Hermetic grant/revoke coverage (#72): the GrantsPanel "Manage access" form mutates the ACL on a
// TABLE. The review, the grant and the revoke are seeded on, and asserted through, the mock catalog's
// per-bearer ledger: the grant carries {user, relation}, the review re-fetches, revoke hits its own
// endpoint.

type Body = Record<string, unknown>;

const TABLE = 'db1%24t'; // the id as it reaches the catalog: encodeURIComponent('db1$t')

const DESCRIBE = {
	version: 1,
	location: 's3://lance-catalog/db1$t',
	schema: { fields: [{ name: 'id', type: 'int64', nullable: false }] },
};

const ACL = {
	object: 'table:db1$t',
	grants: [
		{ relation: 'can_read_data', users: ['user:alice'] },
		{ relation: 'can_write_data', users: ['user:alice'] },
	],
};

/** The table-detail aggregate's upstream parts — the page is gated on `describe`, the rest are
 *  per-part optional (a 404 policy is the honest "none"). */
const DETAIL_ROUTES = {
	[`POST /v1/table/${TABLE}/describe`]: DESCRIBE,
	[`POST /v1/table/${TABLE}/stats`]: { num_rows: 1, total_bytes: 10, num_indices: 0 },
	[`POST /v1/table/${TABLE}/version/list`]: {
		versions: [{ version: 1, timestamp_millis: 1_700_000_000_000, manifest_size: 512 }],
	},
	[`POST /v1/table/${TABLE}/tags/list`]: { tags: {} },
	[`POST /v1/table/${TABLE}/branches/list`]: { branches: {} },
	[`POST /v1/table/${TABLE}/index/list`]: { indexes: [] },
	[`POST /management/v1/table/${TABLE}/policy/describe`]: {
		kind: 'table',
		id: decodeURIComponent(TABLE),
		set: false,
	},
};

let token: string;

const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

const calls = async (page: Page): Promise<Body[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: Body[] }).calls;
};

const callTo = async (page: Page, path: string): Promise<Body | undefined> =>
	(await calls(page)).find((c) => c.path === path);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await mockMe(page);
	await seed(page, {
		...DETAIL_ROUTES,
		[`POST /management/v1/table/${TABLE}/access/list`]: ACL,
		[`POST /management/v1/table/${TABLE}/access/grant`]: {
			object: 'table:db1$t',
			user: 'user:bob',
			relation: 'reader',
			granted: true,
		},
		[`POST /management/v1/table/${TABLE}/access/revoke`]: {
			object: 'table:db1$t',
			user: 'user:bob',
			relation: 'writer',
			granted: false,
		},
	});
});

test('grant writes a base rung and shows the result', async ({ page }) => {
	await page.goto('/catalog/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await expect(page.locator('table.acl')).toContainText('can_read_data');
	await page.getByPlaceholder('principal (idp~user id), or role:…#assignee').last().fill('bob');
	// The manage form's rung picker is the @rask/ui Select (bits-ui) — open by its aria-label, click option.
	await page.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: 'reader', exact: true }).click();
	await page.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect(page.locator('.verdict.ok')).toContainText('granted to');
	expect(await callTo(page, `/management/v1/table/${TABLE}/access/grant`)).toMatchObject({
		method: 'POST',
		body: { user: 'bob', relation: 'reader' },
	});
	// The review re-fetches after the write (the command refreshes it single-flight) — the ACL is
	// still on screen, read back from the server rather than left as the pre-write render.
	await expect(page.locator('table.acl')).toContainText('can_write_data');
});

test('revoke hits the revoke endpoint', async ({ page }) => {
	await page.goto('/catalog/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await page.getByPlaceholder('principal (idp~user id), or role:…#assignee').last().fill('bob');
	await page.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: 'writer', exact: true }).click();
	await page.getByRole('button', { name: 'Revoke', exact: true }).click();
	await expect(page.locator('.verdict.ok')).toContainText('revoked from');
	expect(await callTo(page, `/management/v1/table/${TABLE}/access/revoke`)).toMatchObject({
		method: 'POST',
		body: { user: 'bob', relation: 'writer' },
	});
	// …and never the grant: the two rungs travel to their OWN endpoints.
	expect(await callTo(page, `/management/v1/table/${TABLE}/access/grant`)).toBeUndefined();
});

const BOB = 'user:dex~1b6e2f5a-7c4d-4e9b-9f1a-2d3c4b5a6e7f';

test('a principal is shown by its full id, and its copy control puts the id on the clipboard', async ({
	context,
	page,
}) => {
	await context.grantPermissions(['clipboard-read', 'clipboard-write']);
	await seed(page, {
		[`POST /management/v1/table/${TABLE}/access/list`]: {
			object: 'table:db1$t',
			grants: [{ relation: 'can_read_data', users: [BOB] }],
		},
	});
	await page.goto('/catalog/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	const holders = page.getByRole('row', { name: /can_read_data/ });
	await expect(holders).toContainText(BOB);
	await holders.getByRole('button', { name: `Copy ${BOB}` }).click();
	await expect(holders.getByRole('button', { name: `Copied ${BOB}` })).toBeVisible();
	expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(BOB);
});
