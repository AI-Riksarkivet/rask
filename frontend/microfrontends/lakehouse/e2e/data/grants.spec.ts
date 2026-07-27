import { test, expect, type Route } from '@playwright/test';

// Hermetic grant/revoke coverage (#72): the GrantsPanel "Manage access" form mutates the ACL through the
// /capi BFF. Stub the detail aggregate + access/list + access/grant + access/revoke; assert the grant POST
// carries {user, relation}, the review re-fetches, and revoke hits its own endpoint.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const DETAIL = {
	describe: {
		version: 1,
		location: 's3://lance-catalog/db1$t',
		schema: { fields: [{ name: 'id', type: 'int64', nullable: false }] },
	},
	stats: { num_rows: 1, total_bytes: 10, num_indices: 0 },
	versions: { versions: [{ version: 1, timestamp_millis: 1_700_000_000_000, manifest_size: 512 }] },
	tags: { tags: {} },
	branches: { branches: {} },
	indexes: { indexes: [] },
	policy: null,
};

const ACL = {
	object: 'table:db1$t',
	grants: [
		{ relation: 'can_read_data', users: ['user:alice'] },
		{ relation: 'can_write_data', users: ['user:alice'] },
	],
};

let grantPost: { user: string; relation: string } | null;
let revokePost: { user: string; relation: string } | null;

test.beforeEach(async ({ page }) => {
	grantPost = null;
	revokePost = null;
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path.endsWith('/detail')) return json(route, DETAIL);
		if (path.endsWith('/access/list')) return json(route, ACL);
		if (path.endsWith('/access/grant')) {
			grantPost = req.postDataJSON() as { user: string; relation: string };
			return json(route, {
				object: 'table:db1$t',
				user: `user:${grantPost.user}`,
				relation: grantPost.relation,
				granted: true,
			});
		}
		if (path.endsWith('/access/revoke')) {
			revokePost = req.postDataJSON() as { user: string; relation: string };
			return json(route, {
				object: 'table:db1$t',
				user: `user:${revokePost.user}`,
				relation: revokePost.relation,
				granted: false,
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('grant writes a base rung and shows the result', async ({ page }) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await expect(page.locator('table.acl')).toContainText('can_read_data');
	await page.getByPlaceholder('user (e.g. alice), or role:… / team:…#member').last().fill('bob');
	// The manage form's rung picker is the @rask/ui Select (bits-ui) — open by its aria-label, click option.
	await page.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: 'reader', exact: true }).click();
	await page.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect(page.locator('.verdict.allow')).toContainText('granted to');
	expect(grantPost).toEqual({ user: 'bob', relation: 'reader' });
});

test('revoke hits the revoke endpoint', async ({ page }) => {
	await page.goto('/lakehouse/data/tables/db1%24t');
	await page.getByRole('tab', { name: 'access' }).click();
	await page.getByRole('button', { name: 'Access review' }).click();
	await page.getByPlaceholder('user (e.g. alice), or role:… / team:…#member').last().fill('bob');
	await page.getByLabel('Grant rung').click();
	await page.getByRole('option', { name: 'writer', exact: true }).click();
	await page.getByRole('button', { name: 'Revoke', exact: true }).click();
	await expect(page.locator('.verdict.allow')).toContainText('revoked from');
	expect(revokePost).toEqual({ user: 'bob', relation: 'writer' });
});
