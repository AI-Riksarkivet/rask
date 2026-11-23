import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { postsTo, seed as seedFor } from '../mock-client';
import { MOCK_CATALOG } from '../ports';

// `/projects/<p>` — ONE project's metadata and overview, a top-level page because a project is the
// TOP of the hierarchy. Its read is a `query` running in the browser, so the catalog answer is seeded
// on the mock rather than `page.route`d.
//
// The drill-down goes DOWN a rung at the project→warehouse step: every warehouse row links to its
// `/catalog/warehouses/<id>` page, which this file pins.

const DETAIL = {
	project: 'acme',
	warehouses: [
		{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' },
		{ id: 'acme-gold', bucket: 'gold-bucket', status: 'pending' },
	],
	admins: ['user:alice', 'role:7d2c0f4e-5b1a-4c3e-9f6d-2a8b1c0e9f3a#assignee'],
};

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, { 'GET /management/v1/me': ME_ADMIN });
});

test('renders the project’s warehouse rows and effective admins', async ({ page }) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
	await page.goto('/projects/acme');
	await expect(page.getByRole('heading', { name: 'acme', exact: true })).toBeVisible();

	// Every registry fact the row carries — id, bucket and the status chip, whose non-active value is
	// the only thing separating a provisioned warehouse from one that is still coming up.
	const work = page.getByRole('row').filter({ hasText: 'acme-wh' });
	await expect(work).toContainText('acme-bucket');
	await expect(work).toContainText('active');
	const gold = page.getByRole('row').filter({ hasText: 'acme-gold' });
	await expect(gold).toContainText('gold-bucket');
	await expect(gold).toContainText('pending');

	// The effective admins, verbatim — a userset (`role:<id>#assignee`) is a real grant and must not be
	// flattened into a plain user.
	await expect(page.getByText('user:alice', { exact: true })).toBeVisible();
	await expect(
		page.getByText('role:7d2c0f4e-5b1a-4c3e-9f6d-2a8b1c0e9f3a#assignee', { exact: true }),
	).toBeVisible();
});

test('each warehouse row links to its catalog page', async ({ page }) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
	await page.goto('/projects/acme');
	for (const id of ['acme-wh', 'acme-gold']) {
		const link = page.getByRole('link', { name: id, exact: true });
		await expect(link).toHaveAttribute('href', `/catalog/warehouses/${id}`);
	}
	// The way back up is the breadcrumb's, as on every page.
	const back = page.getByRole('navigation', { name: 'Breadcrumb' }).getByRole('link', {
		name: 'Projects',
	});
	await expect(back).toHaveAttribute('href', '/projects');
	// …and the catalog's lists open filtered to this project, the filter in the URL.
	const lists = page.getByRole('navigation', { name: 'This project in the catalog' });
	for (const list of ['warehouses', 'namespaces', 'tables']) {
		await expect(lists.locator(`a[href="/catalog/${list}?project=acme"]`)).toBeVisible();
	}
});

test('a project the reader cannot see reads as missing or not theirs, never as an empty project', async ({
	page,
}) => {
	// The catalog answers a non-member exactly as it answers a missing project: 404, code 4.
	await page.route('**/api/catalog/management/v1/projects/secret', (route) =>
		route.fulfill({
			status: 404,
			contentType: 'application/problem+json',
			body: JSON.stringify({ code: 4, detail: 'project not found: secret' }),
		}),
	);
	const sent: string[] = [];
	page.on('request', (req) => sent.push(new URL(req.url()).pathname));
	await page.goto('/projects/secret');
	await expect(
		page.getByText('that you can see: it does not exist, or you hold no grant on it.'),
	).toBeVisible();
	// Nothing is read for a project that is not there: no policies, no events.
	expect(sent.filter((p) => p.endsWith('/projects/secret/policies'))).toEqual([]);
	expect(sent.filter((p) => p.endsWith('/management/v1/events'))).toEqual([]);
	await expect(page.locator('.empty code', { hasText: 'secret' })).toBeVisible();
	// An honest answer, not a project that looks like it has nothing in it.
	await expect(page.getByRole('heading', { name: 'Warehouses' })).toHaveCount(0);
});

test('a visit to a project reads the project once', async ({ page }) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
	const reads: string[] = [];
	page.on('request', (req) => {
		if (new URL(req.url()).pathname.endsWith('/management/v1/projects/acme')) reads.push(req.url());
	});
	await page.goto('/projects/acme');
	// The events panel is the page's last read, made after the project's.
	await expect(
		page.getByText(/No governance changes on this project|Events unavailable right now/),
	).toBeVisible();
	expect(reads).toHaveLength(1);
});

test('a project admin defines a label on the project page, and evolves it', async ({ page }) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
	await page.goto('/projects/acme');
	const labels = page.getByTestId('project-labels');
	await expect(labels).toContainText('This project defines no label yet.');

	const form = labels.getByRole('form', { name: 'Define a label' });
	await form.getByLabel('Label name').fill('sensitivity');
	await form.getByLabel('Label kind').click();
	await page.getByRole('option', { name: 'one of a list' }).click();
	// Nothing to submit until the label says what it applies to and what values it allows.
	await expect(form.getByRole('button', { name: 'Define label' })).toBeDisabled();
	await form.getByLabel('Allowed values').fill('internal, restricted');
	await form.getByLabel('Label description').fill('How tightly the data is held');
	await form.getByRole('button', { name: 'Define label' }).click();

	const row = labels.getByRole('row').filter({ hasText: 'sensitivity' });
	await expect(row).toContainText('one of a list');
	await expect(row).toContainText('tables');
	await expect(row).toContainText('internal, restricted');
	await expect(row).toContainText('How tightly the data is held');

	// Evolving it may only grow it: the kind is fixed, and it may now apply to namespaces too.
	await row.getByRole('button', { name: 'Evolve label sensitivity' }).click();
	const evolve = labels.getByRole('form', { name: 'Evolve label sensitivity' });
	await expect(evolve.getByLabel('Label kind')).toBeDisabled();
	await evolve.getByRole('checkbox', { name: 'namespaces' }).click();
	await evolve.getByRole('button', { name: 'Save label' }).click();
	await expect(row).toContainText('namespaces, tables');
});

test('a label still attached is not deleted, and the catalog’s reason is said', async ({
	page,
}) => {
	await seed(page, { 'GET /management/v1/projects/acme': DETAIL });
	await page.request.post(`${MOCK_CATALOG}/__mock/labels`, {
		data: {
			bearer: token,
			definitions: [{ project: 'acme', name: 'pii', scope: ['table'], kind: 'marker' }],
			objects: [{ kind: 'table', id: 'acme-wh$raw$a', project: 'acme', labels: { pii: null } }],
		},
	});
	await page.goto('/projects/acme');
	const labels = page.getByTestId('project-labels');
	await labels.getByRole('button', { name: 'Delete label pii' }).click();
	await expect(labels.getByRole('status')).toHaveText("label 'pii' is still attached to 1 object");
	await expect(labels.getByRole('row').filter({ hasText: 'pii' })).toBeVisible();
});

test('a define the catalog refuses for want of project-admin access says so', async ({ page }) => {
	await seed(page, {
		'GET /management/v1/projects/acme': DETAIL,
		'POST /management/v1/projects/acme/labels/define': {
			status: 403,
			body: { detail: 'user:alice lacks can_administer on project:acme' },
		},
	});
	await page.goto('/projects/acme');
	const form = page.getByTestId('project-labels').getByRole('form', { name: 'Define a label' });
	await form.getByLabel('Label name').fill('pii');
	await form.getByRole('button', { name: 'Define label' }).click();
	await expect(page.getByTestId('project-labels').getByRole('status')).toHaveText(
		'Denied: defining a label needs project-admin access (can_administer).',
	);
});

test('a project admin reviews, grants and revokes access on the project', async ({ page }) => {
	const grant = { object: 'project:acme', user: 'dex~bob', relation: 'describer' };
	await seed(page, {
		'GET /management/v1/projects/acme': DETAIL,
		'POST /management/v1/projects/acme/access/list': {
			object: 'project:acme',
			grants: [
				{ relation: 'can_read_assignments', users: ['user:e2e'] },
				{ relation: 'can_decide_access', users: [] },
			],
		},
		'POST /management/v1/projects/acme/access/my-permissions': {
			object: 'project:acme',
			subject: 'user:e2e',
			permissions: { can_grant_describer: true, can_revoke_grant: true },
		},
		'POST /management/v1/projects/acme/access/grant': { ...grant, granted: true },
		'POST /management/v1/projects/acme/access/revoke': { ...grant, granted: false },
	});
	await page.goto('/projects/acme');
	const access = page.locator('section', { has: page.getByRole('heading', { name: 'Access' }) });
	await access.getByRole('button', { name: 'Access review' }).click();
	await expect(access.getByRole('row').filter({ hasText: 'can_read_assignments' })).toContainText(
		'user:e2e',
	);
	// A project has no check door, so no simulator is offered.
	await expect(access.getByText('Check a specific access')).toHaveCount(0);

	await access.getByLabel('Subject to grant to').fill('dex~bob');
	await access.getByLabel('Grant rung').click();
	// Exactly the rungs the project's grant door takes.
	await expect(page.getByRole('option')).toHaveText([/^describer/, /^modifier/, /^decider/]);
	await page.getByRole('option', { name: /^describer/ }).click();
	await access.getByRole('button', { name: 'Grant', exact: true }).click();
	await expect(access.locator('.verdict.ok')).toHaveText('describer granted to dex~bob.');
	expect(await postsTo(page, token, '/management/v1/projects/acme/access/grant')).toEqual([
		{ user: 'dex~bob', relation: 'describer' },
	]);

	await access.getByRole('button', { name: 'Revoke', exact: true }).click();
	await expect(access.locator('.verdict.ok')).toHaveText('describer revoked from dex~bob.');
	expect(await postsTo(page, token, '/management/v1/projects/acme/access/revoke')).toEqual([
		{ user: 'dex~bob', relation: 'describer' },
	]);
});
