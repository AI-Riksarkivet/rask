import { expect, test, type Page } from '@playwright/test';
import { MOCK_CATALOG } from './ports';
import { signIn, TOKEN } from './session';
import { cellUnder } from '../table-cell';

// The catalog lists are estate-wide; each has a project column and a `?project=` filter. Each test
// signs in on a bearer of its own (an estate admin who is a member of `acme`), so the world it seeds,
// a failing read included, is its alone.
let bearer: string;

const WAREHOUSE = (id: string, project: string) => ({
	id,
	project,
	bucket: `${id}-bucket`,
	root_uri: `s3://${id}-bucket`,
	status: 'active',
});

const HOLD = (table: string) => ({
	table,
	uuid: `uuid-${table}`,
	version: 3,
	published_version: 2,
	reasons: ['row_count_delta'],
	opened_at: '2026-11-01T10:00:00Z',
	expires_at: '2026-11-04T10:00:00Z',
	state: 'open',
});

test.beforeEach(async ({ context, request }, testInfo) => {
	bearer = `${TOKEN.admin}:project-filter:${testInfo.testId}`;
	await signIn(context, { token: bearer });
	await request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer,
			routes: {
				'GET /management/v1/warehouses': [
					WAREHOUSE('acme-wh', 'acme'),
					WAREHOUSE('globex-wh', 'globex'),
				],
				'GET /management/v1/warehouses/-/bindings': {
					bindings: { db1: 'acme-wh', db2: 'globex-wh' },
				},
				'GET /v1/table': { tables: ['db1$t', 'db2$u', 'provenance$events'] },
				'GET /management/v1/promotions?limit=100': {
					holds: [HOLD('db1$t'), HOLD('db2$u')],
					page_token: null,
				},
				// The projects the caller can see: `globex` is one this caller holds no role on, and
				// `initech` one with no warehouse, so nothing in any list.
				'GET /management/v1/projects': [
					{ project: 'acme', warehouses: [{ id: 'acme-wh' }] },
					{ project: 'globex', warehouses: [{ id: 'globex-wh' }] },
					{ project: 'initech', warehouses: [] },
				],
			},
		},
	});
});

/** Choose a project in the list's filter. */
async function filterBy(page: Page, label: string): Promise<void> {
	await page.getByLabel('Project filter').click();
	await page.getByRole('option', { name: label }).click();
}

/** The filter's whole contract on one list: it narrows the rows, it is in the URL, it survives a
 *  reload (and the sign-in a reload brings), and choosing every project clears the parameter. */
async function proveFilter(page: Page, path: string, acmeRow: string, otherRow: string) {
	await page.goto(path);
	await expect(page.getByText(acmeRow, { exact: true })).toBeVisible();
	await expect(page.getByText(otherRow, { exact: true })).toBeVisible();

	await filterBy(page, 'acme');
	await expect(page).toHaveURL(/[?&]project=acme(&|$)/);
	await expect(page.getByText(acmeRow, { exact: true })).toBeVisible();
	await expect(page.getByText(otherRow, { exact: true })).toHaveCount(0);

	await page.reload();
	await expect(page.getByText(acmeRow, { exact: true })).toBeVisible();
	await expect(page.getByText(otherRow, { exact: true })).toHaveCount(0);

	await filterBy(page, 'all projects');
	await expect(page).not.toHaveURL(/project=/);
	await expect(page.getByText(otherRow, { exact: true })).toBeVisible();
}

test('the warehouses list filters by project, in the URL, over every project the caller can see', async ({
	page,
}) => {
	await proveFilter(page, '/catalog/warehouses', 'acme-wh', 'globex-wh');
	// The choices are the catalog's project list, not only the projects the caller belongs to.
	await filterBy(page, 'globex');
	await expect(page.getByText('globex-wh', { exact: true })).toBeVisible();
	await expect(page.getByText('acme-wh', { exact: true })).toHaveCount(0);
});

test('the namespaces list filters by project, in the URL', async ({ page }) => {
	await proveFilter(page, '/catalog/namespaces', 'db1', 'db2');
});

test('the tables list filters by project, in the URL, and a table with no project shows a dash', async ({
	page,
}) => {
	await proveFilter(page, '/catalog/tables', 'db1$t', 'db2$u');
	await expect(await cellUnder(page, 'provenance$events', 'project')).toHaveText('—');
});

test('the promotions list filters by project, in the URL', async ({ page }) => {
	await proveFilter(page, '/catalog/promotions', 'db1$t', 'db2$u');
});

test('a filter the list cannot apply says why and offers to clear it, and an empty match names the project', async ({
	page,
	request,
}) => {
	await page.goto('/catalog/tables?project=initech');
	await expect(page.getByText('No tables in project initech.')).toBeVisible();
	await expect(page.getByText('No tables registered')).toHaveCount(0);

	await request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer,
			routes: {
				'GET /management/v1/warehouses/-/bindings': {
					status: 503,
					body: { detail: 'bindings unavailable' },
				},
			},
		},
	});
	await page.goto('/catalog/tables?project=acme');
	const notice = page.getByRole('alert').filter({ hasText: 'cannot be applied' });
	await expect(notice).toContainText('the namespace bindings read answered HTTP 503');
	// No rows at all, rather than every row shown as though it were filtered.
	await expect(page.getByText('db1$t', { exact: true })).toHaveCount(0);
	await notice.getByRole('button', { name: 'Show all projects' }).click();
	await expect(page).not.toHaveURL(/project=/);
	await expect(page.getByText('db2$u', { exact: true })).toBeVisible();
});

test('promotions filtered to a project with no hold say so, and say the filter reads only the holds loaded', async ({
	page,
}) => {
	await page.goto('/catalog/promotions?project=initech');
	await expect(
		page.getByText('No promotion of project initech is waiting for a decision.'),
	).toBeVisible();
	await expect(page.getByTestId('promotions-filter-scope')).toHaveText(
		'Among the 2 holds loaded, which are all of them.',
	);
	await expect(page.getByTestId('promotions')).toHaveCount(0);
});

test('a filter naming no project the reader can see, and no row, says so and offers every project', async ({
	page,
}) => {
	// An id the catalog does not answer is not "a project with no tables": it is none the reader can
	// see, which the console cannot tell from one that does not exist, and says neither.
	await page.goto('/catalog/tables?project=nope');
	const notice = page.getByTestId('project-filter-notice');
	await expect(notice).toContainText(
		'No project nope that you can see: it does not exist, or you hold no grant on it.',
	);
	await expect(page.getByText('No tables in project nope.')).toHaveCount(0);
	// The select keeps naming the filter, as a choice that is no project.
	await page.getByLabel('Project filter').click();
	await expect(page.getByRole('option', { name: 'nope (none you can see)' })).toBeDisabled();
	await page.keyboard.press('Escape');
	await notice.getByRole('button', { name: 'Show all projects' }).click();
	await expect(page).not.toHaveURL(/project=/);
	await expect(page.getByText('db2$u', { exact: true })).toBeVisible();
});

test('a filter to a project the reader holds rows in, though no member of it, shows those rows', async ({
	page,
	request,
}) => {
	// The projects read names only the projects the reader is a member of; a grant on a table in
	// another project gives them its rows all the same, and the filter must not take them away.
	await request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer,
			routes: { 'GET /management/v1/projects': [{ project: 'acme', warehouses: [] }] },
		},
	});
	await page.goto('/catalog/tables?project=globex');
	await expect(page.getByText('db2$u', { exact: true })).toBeVisible();
	await expect(page.getByText('db1$t', { exact: true })).toHaveCount(0);
	await expect(page.getByTestId('project-filter-notice')).toHaveCount(0);
});

test('a filter the projects read could not check says why, rather than that the project is empty', async ({
	page,
	request,
}) => {
	await request.post(`${MOCK_CATALOG}/__mock/seed`, {
		data: {
			bearer,
			routes: {
				'GET /management/v1/projects': { status: 503, body: { detail: 'projects unavailable' } },
			},
		},
	});
	await page.goto('/catalog/tables?project=nope');
	await expect(page.getByTestId('project-filter-notice')).toContainText(
		'the projects read answered HTTP 503',
	);
	await expect(page.getByText('No tables in project nope.')).toHaveCount(0);
});

test('the subtitle says what the rows are while a filter is set', async ({ page }) => {
	await page.goto('/catalog/tables');
	await expect(page.getByText(/estate-wide/)).toBeVisible();
	await filterBy(page, 'acme');
	await expect(page.getByText(/estate-wide/)).toHaveCount(0);
	await expect(page.getByText(/in project acme/)).toBeVisible();
});

test('each filter change is a history entry, so Back is the list as it was', async ({ page }) => {
	// Into the list from another page, so the entry before it is not the sign-in round trip's.
	await page.goto('/catalog/warehouses');
	await expect(page.getByText('acme-wh', { exact: true })).toBeVisible();
	await page
		.getByRole('navigation', { name: 'Pages' })
		.getByRole('link', { name: 'Tables', exact: true })
		.click();
	await expect(page.getByText('db2$u', { exact: true })).toBeVisible();
	await filterBy(page, 'acme');
	await expect(page).toHaveURL(/project=acme/);
	await page.goBack();
	await expect(page).toHaveURL(/\/catalog\/tables$/);
	await expect(page.getByText('db2$u', { exact: true })).toBeVisible();
});

test('the project select is labelled on screen', async ({ page }) => {
	await page.goto('/catalog/tables');
	await expect(
		page.locator('label', { hasText: 'Project' }).getByLabel('Project filter'),
	).toBeVisible();
});
