import { expect, test, type Page, type Route } from '@playwright/test';
import { MOCK_CATALOG } from './ports';
import { ME_ADMIN, ME_MEMBER, mockMe, signIn, TOKEN } from './session';

// Home reads the doors the caller already reads (ADR 0210): the catalog's tables and warehouses,
// seeded per bearer on the mock catalog; lineage's feed and DLQ, and the catalog's control
// events, answered in the browser. An estate admin sees the DLQ and the governance changes; anyone else
// gets neither, and the console does not ask for them.

let bearer: string;

const WAREHOUSE = (id: string, project: string) => ({
	id,
	project,
	bucket: `${id}-bucket`,
	root_uri: `s3://${id}-bucket`,
	status: 'active',
});

const json = (route: Route, body: unknown) =>
	route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

const COMMIT = {
	seq: 9,
	event_type: 'COMPLETE',
	event_time: '2026-11-15T19:11:53Z',
	job: 'lance-catalog/insert.acme-wh$bronze$orders',
	author: 'dex~writer',
	outputs: ['acme-wh$bronze$orders'],
};

const GRANT_EVENT = {
	event_id: 'e1',
	occurred_at: '2026-11-15T19:00:00Z',
	action: 'table_created',
	object_type: 'table',
	object_id: 'acme-wh$bronze$orders',
	actor: 'dex~writer',
	extra: {},
};

/** The world every test reads; `dlqAsked` records whether the page asked lineage for the DLQ. */
async function seedWorld(page: Page): Promise<{ dlqAsked: () => boolean }> {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer,
			routes: {
				'GET /management/v1/warehouses': [
					WAREHOUSE('acme-wh', 'acme'),
					WAREHOUSE('globex-wh', 'globex'),
				],
				'GET /management/v1/projects': [{ project: 'acme', warehouses: [{ id: 'acme-wh' }] }],
				'GET /v1/table': {
					tables: [
						'acme-wh$bronze$orders',
						'acme-wh$gold$catalog',
						'globex-wh$silver$u',
						'lance_catalog$provenance$events',
					],
				},
			},
		},
	});
	await page.route('**/api/lineage/events?*', (route) =>
		json(route, { events: [COMMIT], next_cursor: COMMIT.seq }),
	);
	let asked = false;
	await page.route('**/api/lineage/admin/dlq?*', (route) => {
		asked = true;
		return json(route, { depth: 3, events: [], limit: 1, oldest_age_seconds: 40 });
	});
	await page.route('**/api/catalog/management/v1/events?*', (route) =>
		json(route, { events: [GRANT_EVENT], cursor: 1, reset: false }),
	);
	return { dlqAsked: () => asked };
}

test.beforeEach(async ({ context }, testInfo) => {
	bearer = `${TOKEN.admin}:home:${testInfo.testId}`;
	await signIn(context, { token: bearer });
});

test('an estate admin’s Home counts their tables per project, and shows commits, dead letters and governance', async ({
	page,
}) => {
	await mockMe(page, ME_ADMIN);
	await seedWorld(page);
	await page.goto('/');

	await expect(page.getByTestId('home-signed-in')).toHaveText(`Signed in as ${ME_ADMIN.name}`);

	const tables = page.getByTestId('home-tables');
	// project, then its tables
	await expect(tables.getByRole('columnheader')).toHaveText(['project', 'tables']);
	await expect(tables.locator('tbody tr').nth(0)).toHaveText(/acme\s*2/);
	await expect(tables.locator('tbody tr').nth(1)).toHaveText(/globex\s*1/);
	await expect(tables.locator('tbody tr').nth(2)).toHaveText(/the estate’s own\s*1/);
	await expect(tables.getByRole('link', { name: 'acme', exact: true })).toHaveAttribute(
		'href',
		'/catalog/tables?project=acme',
	);

	const commits = page.getByTestId('home-commits');
	await expect(commits).toContainText('insert');
	await expect(commits.getByRole('link', { name: 'acme-wh › bronze › orders' })).toHaveAttribute(
		'href',
		'/catalog/tables/acme-wh%24bronze%24orders',
	);

	await expect(page.getByTestId('home-dlq')).toHaveText('3');
	await expect(page.getByTestId('home-governance')).toContainText('table created');
});

test('a non-admin’s Home shows their own figures and asks for no admin read', async ({ page }) => {
	await mockMe(page, ME_MEMBER);
	const world = await seedWorld(page);
	await page.goto('/');

	await expect(page.getByTestId('home-signed-in')).toHaveText(`Signed in as ${ME_MEMBER.name}`);
	const tables = page.getByTestId('home-tables');
	await expect(tables.locator('tbody tr')).toHaveCount(3);
	await expect(page.getByTestId('home-attention')).toHaveCount(0);
	await expect(page.getByTestId('home-dlq')).toHaveCount(0);
	await expect(page.getByTestId('home-governance')).toHaveCount(0);
	expect(world.dlqAsked()).toBe(false);
});
