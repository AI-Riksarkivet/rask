import { test, expect, type Page, type Route } from '@playwright/test';
import { signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic project-creation coverage (goal cond 6): the estate-admin flow COMPOSES existing APIs —
// warehouse create (the project comes into existence with its first warehouse), the optional
// serving:"gold" second create, and the initial admin tuple. The warehouse create and the estate list
// now ride `warehouses.remote.ts`, and the admin grant the shared `writeTuple` command, so ALL THREE
// run on the zone SERVER and land on the mock catalog: the reads are seeded per bearer, the warehouse
// POSTs are read back from `/__mock/calls`, and the tuple write from `/__mock/access`. Only `/v1/me`
// still crosses the browser (the OIDC/BFF identity plane stays there), so it alone is `page.route`d.
// Each test signs in with a per-test bearer so the fullyParallel suite shares no mutable mock state.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const ME_ALICE = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [],
};

/** A created warehouse, in the seed's explicit {status, body} form: a bare record carrying its own
 *  `status` key would be read AS that form, and `"active"` would become the HTTP status. */
const created = (id: string, project: string, bucket: string) => ({
	status: 200,
	body: { id, project, bucket, root_uri: `s3://${bucket}`, status: 'active' },
});

let token: string;
let me: Record<string, unknown>;

/** Pre-seed exact catalog responses for THIS test's bearer ("METHOD /path" → body). */
const seed = async (page: Page, routes: Record<string, unknown>): Promise<void> => {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
};

/** The warehouse creates the mock catalog recorded for THIS test's bearer, in order. */
const warehousePosts = async (page: Page): Promise<unknown[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	const { calls } = (await res.json()) as {
		calls: Array<{ method: string; path: string; body: unknown }>;
	};
	return calls.filter((c) => c.method === 'POST' && c.path === '/v1/warehouses').map((c) => c.body);
};

/** The tuple writes the mock catalog recorded for THIS test's bearer. */
const writtenTuples = async (page: Page): Promise<unknown[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/access`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { written: unknown[] }).written;
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	me = { ...ME_ALICE };
	await page.route('**/capi/v1/me', (route) => json(route, me));
	// The estate starts empty; each test seeds the create's outcome, and the one that asserts the
	// gallery re-seeds the list its post-create re-read lands on.
	await seed(page, { 'GET /v1/projects': [] });
});

test('creates work + gold warehouses and grants the initial admin, with the exact wire bodies', async ({
	page,
}) => {
	await seed(page, { 'POST /v1/warehouses': created('acme-wh', 'acme', 'acme-wh') });
	await page.goto('/lakehouse/catalog/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('acme');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('acme-wh');
	await dialog.getByLabel('Gold serving warehouse id').fill('acme-gold');
	// the admin subject prefilled from /v1/me (user:alice) is kept
	await expect(dialog.getByLabel('Initial admin')).toHaveValue('user:alice');
	// The estate AFTER the creates — the create makes the project visible on the next estate list,
	// which is what the command's single-flight refresh and the `oncreated` reload both read.
	await seed(page, {
		'GET /v1/projects': [
			{
				project: 'acme',
				warehouses: [
					{ id: 'acme-wh', bucket: 'acme-wh', status: 'active' },
					{ id: 'acme-gold', bucket: 'acme-gold', status: 'active', serving: 'gold' },
				],
				admins: [],
			},
		],
	});
	await dialog.getByRole('button', { name: 'Create project' }).click();
	// wire bodies pinned: the work create carries NO serving key; the gold one carries serving:"gold"
	await expect
		.poll(async () => warehousePosts(page))
		.toEqual([
			{ id: 'acme-wh', project: 'acme', bucket: null },
			{ id: 'acme-gold', project: 'acme', bucket: null, serving: 'gold' },
		]);
	// the initial admin grant is one raw FGA tuple on the new project object — recorded by the mock
	// catalog, because the write runs server-side through the shared remote command
	await expect
		.poll(async () => writtenTuples(page))
		.toEqual([{ user: 'user:alice', relation: 'admin', object: 'project:acme' }]);
	// success toast + the gallery reflects the new project on the reload oncreated triggered
	await expect(
		page.getByText(
			'Project acme created — warehouse acme-wh, gold serving acme-gold, admin user:alice.',
		),
	).toBeVisible();
	await expect(page.locator('a.row', { hasText: 'acme' })).toContainText('2 warehouses');
});

test('gold warehouse and admin grant are optional — one create, no tuple', async ({ page }) => {
	await seed(page, { 'POST /v1/warehouses': created('solo-wh', 'solo', 'solo-bucket') });
	await page.goto('/lakehouse/catalog/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('solo');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('solo-wh');
	await dialog.getByLabel('Warehouse bucket').fill('solo-bucket');
	await dialog.getByLabel('Initial admin').clear();
	await dialog.getByRole('button', { name: 'Create project' }).click();
	await expect
		.poll(async () => warehousePosts(page))
		.toEqual([{ id: 'solo-wh', project: 'solo', bucket: 'solo-bucket' }]);
	// the toast marks the END of the flow — only then is "no tuple ever fired" a settled fact
	await expect(page.getByText('Project solo created — warehouse solo-wh.')).toBeVisible();
	expect(await writtenTuples(page)).toEqual([]);
});

test('a denied first create toasts the failure and keeps the dialog open for a retry', async ({
	page,
}) => {
	await seed(page, { 'POST /v1/warehouses': { status: 403, body: { detail: 'forbidden' } } });
	await page.goto('/lakehouse/catalog/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('acme');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('acme-wh');
	await dialog.getByRole('button', { name: 'Create project' }).click();
	// nothing was created — the dialog stays open with the inline error, and no grant ever fired
	await expect(dialog).toContainText(
		'Denied: provisioning warehouse acme-wh needs the estate/project-admin rung.',
	);
	expect(await writtenTuples(page)).toEqual([]);
	await expect(page.getByRole('dialog')).toBeVisible();
});

test('a failed admin grant is a NAMED partial outcome, not a fake success', async ({ page }) => {
	// The failure lever lives on the mock catalog now (the write is server-side — page.route cannot
	// reach it): this test's bearer gets failWrites, nobody else's does.
	await seed(page, { 'POST /v1/warehouses': created('acme-wh', 'acme', 'acme-wh') });
	await page.request.post(`${MOCK_CATALOG}/__mock/access/config`, {
		data: { bearer: token, failWrites: true },
	});
	await page.goto('/lakehouse/catalog/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('acme');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('acme-wh');
	await dialog.getByRole('button', { name: 'Create project' }).click();
	await expect(
		page.getByText(/Project acme created with acme-wh, but admin grant for user:alice failed/),
	).toBeVisible();
});

test('the creation flow is invisible to a non-estate-admin (the /v1/me gate)', async ({ page }) => {
	me = { ...ME_ALICE, sub: 'user:bob', name: 'Bob', estate_admin: false };
	await page.goto('/lakehouse/catalog/projects');
	// the gallery renders (empty estate list) but the admin affordance never does
	await expect(page.getByText('No projects visible')).toBeVisible();
	await expect(page.getByRole('button', { name: 'New project' })).toHaveCount(0);
});
