import { test, expect, type Route } from '@playwright/test';
import { signIn, TOKEN } from './session';
import { MOCK_CATALOG } from '../ports';

// Hermetic project-creation coverage (goal cond 6): the estate-admin flow COMPOSES existing APIs —
// warehouse create (the project comes into existence with its first warehouse), the optional
// serving:"gold" second create, and the initial admin tuple. The warehouse/project/me reads stay
// stubbed at the browser boundary (they are still BFF calls); the ADMIN GRANT is not — it rides the
// shared `writeTuple` remote command, which runs on the zone SERVER, so it lands on the mock catalog
// and is asserted through `/__mock/access` per bearer. Lives in the admin project (auth-ON server)
// because that server is the one whose CATALOG_API points at the mock; each test signs in with a
// per-test bearer so the fullyParallel suite shares no mutable mock state.

const json = (route: Route, body: unknown, status = 200) =>
	route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const ME_ALICE = {
	sub: 'user:alice',
	name: 'Alice',
	email: 'alice@example.com',
	estate_admin: true,
	projects: [],
};

let token: string;
let me: Record<string, unknown>;
let projects: Array<Record<string, unknown>>;
let warehousePosts: Array<Record<string, unknown>>;
let warehouseStatus: number;

/** The tuple writes the mock catalog recorded for THIS test's bearer. */
const writtenTuples = async (page: import('@playwright/test').Page): Promise<unknown[]> => {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/access`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { written: unknown[] }).written;
};

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	me = { ...ME_ALICE };
	projects = [];
	warehousePosts = [];
	warehouseStatus = 200;
	await page.route('**/capi/**', (route) => {
		const req = route.request();
		const path = new URL(req.url()).pathname.replace(/^.*\/capi/, '');
		if (path === '/v1/me') return json(route, me);
		if (path === '/v1/projects') return json(route, projects);
		if (path === '/v1/warehouses' && req.method() === 'POST') {
			const body = req.postDataJSON() as Record<string, unknown> & {
				id: string;
				project: string;
				bucket: string | null;
			};
			warehousePosts.push(body);
			if (warehouseStatus !== 200) return json(route, { detail: 'forbidden' }, warehouseStatus);
			// the create makes the project visible on the next estate list — mirror that
			const existing = projects.find((p) => p.project === body.project);
			const wh = { id: body.id, bucket: body.bucket ?? body.id, status: 'active' };
			if (existing) (existing.warehouses as unknown[]).push(wh);
			else projects.push({ project: body.project, warehouses: [wh], admins: [] });
			return json(route, {
				id: body.id,
				project: body.project,
				bucket: body.bucket ?? body.id,
				root_uri: `s3://${body.bucket ?? body.id}`,
				status: 'active',
			});
		}
		return json(route, { detail: 'unstubbed' }, 404);
	});
});

test('creates work + gold warehouses and grants the initial admin, with the exact wire bodies', async ({
	page,
}) => {
	await page.goto('/lakehouse/catalog/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('acme');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('acme-wh');
	await dialog.getByLabel('Gold serving warehouse id').fill('acme-gold');
	// the admin subject prefilled from /v1/me (user:alice) is kept
	await expect(dialog.getByLabel('Initial admin')).toHaveValue('user:alice');
	await dialog.getByRole('button', { name: 'Create project' }).click();
	// wire bodies pinned: the work create carries NO serving key; the gold one carries serving:"gold"
	await expect
		.poll(() => warehousePosts)
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
	await page.goto('/lakehouse/catalog/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('solo');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('solo-wh');
	await dialog.getByLabel('Warehouse bucket').fill('solo-bucket');
	await dialog.getByLabel('Initial admin').clear();
	await dialog.getByRole('button', { name: 'Create project' }).click();
	await expect
		.poll(() => warehousePosts)
		.toEqual([{ id: 'solo-wh', project: 'solo', bucket: 'solo-bucket' }]);
	// the toast marks the END of the flow — only then is "no tuple ever fired" a settled fact
	await expect(page.getByText('Project solo created — warehouse solo-wh.')).toBeVisible();
	expect(await writtenTuples(page)).toEqual([]);
});

test('a denied first create toasts the failure and keeps the dialog open for a retry', async ({
	page,
}) => {
	warehouseStatus = 403;
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
