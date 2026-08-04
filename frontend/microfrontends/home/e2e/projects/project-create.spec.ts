import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, ME_MEMBER, signIn, TOKEN } from './session';
import { postsTo, seed as seedFor } from './mock-client';

// Hermetic project-creation coverage, moved here with the surface (2026-08-03 ruling — a project is the
// TOP of the hierarchy, so the page that MINTS one lives beside the estate's project list in the main
// menu). Ported from the lakehouse zone's `e2e/admin/project-create.spec.ts` test for test: the flow
// COMPOSES existing APIs — warehouse create (the project comes into existence with its first
// warehouse), the optional serving:"gold" second create, and the initial admin tuple — and all three
// run on the zone SERVER through remote functions, so all three land on the mock catalog: reads seeded
// per bearer, writes read back from `/__mock/calls`.
//
// The one mechanical difference from the lakehouse original: `/v1/me` is seeded on the mock rather than
// `page.route`d. Home's `+layout.server.ts` calls `fetchMe` against CATALOG_API directly — there is no
// `/capi/v1/me` browser hop to intercept — so the identity has to ride in something the SERVER sees,
// which is the bearer. Each test signs in with a per-test bearer, so the fullyParallel suite shares no
// mutable mock state.

/** A created warehouse, in the seed's explicit {status, body} form: a bare record carrying its own
 *  `status` key would be read AS that form, and `"active"` would become the HTTP status. */
const created = (id: string, project: string, bucket: string) => ({
	status: 200,
	body: { id, project, bucket, root_uri: `s3://${bucket}`, status: 'active' },
});

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

/** The warehouse creates the mock catalog recorded for THIS test's bearer, in order. */
const warehousePosts = (page: Page): Promise<unknown[]> => postsTo(page, token, '/v1/warehouses');

/** The tuple writes the mock catalog recorded for THIS test's bearer. Recorded even when unseeded, so
 *  "no grant ever fired" is a fact about the app, not about the seed. */
const writtenTuples = (page: Page): Promise<unknown[]> => postsTo(page, token, '/v1/access/tuples');

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	// The estate starts empty; each test seeds the create's outcome, and the one that asserts the
	// gallery re-seeds the list its post-create re-read lands on.
	await seed(page, {
		'GET /v1/me': ME_ADMIN,
		'GET /v1/projects': [],
		// The FGA write door answers OK by default — the real catalog echoes the tuple it wrote, and a
		// static stand-in only has to PARSE (the wire body that matters is read back from the ledger,
		// not from this). Left unseeded it 404s, and the flow then honestly reports a partial outcome
		// where the test meant to exercise the whole-success path. The partial test overrides it.
		'POST /v1/access/tuples': { user: 'user:alice', relation: 'admin', object: 'project:acme' },
	});
});

test('creates work + gold warehouses and grants the initial admin, with the exact wire bodies', async ({
	page,
}) => {
	await seed(page, { 'POST /v1/warehouses': created('acme-wh', 'acme', 'acme-wh') });
	await page.goto('/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('acme');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('acme-wh');
	await dialog.getByLabel('Gold serving warehouse id').fill('acme-gold');
	// the admin subject prefilled from /v1/me (user:alice) is kept
	await expect(dialog.getByLabel('Initial admin')).toHaveValue('user:alice');
	// The estate AFTER the creates — the create makes the project visible on the next estate list,
	// which is what the dialog's `oncreated` invalidateAll re-reads.
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
	// The success toast FIRST, unlike the lakehouse original — it is the only EPHEMERAL assertion here
	// (sonner auto-dismisses after ~4s), and it is also the end-of-flow signal, so every ledger read
	// after it is a settled fact. Queued behind the two polls' HTTP round-trips it had already
	// dismissed by the time it was looked for: a real failure of the test, not of the product.
	await expect(
		page.getByText(
			'Project acme created — warehouse acme-wh, gold serving acme-gold, admin user:alice.',
		),
	).toBeVisible();
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
	// the gallery reflects the new project on the reload oncreated triggered
	// SAME-ZONE card link now (`/projects/acme`, not `/lakehouse/catalog/projects/acme`) — the overview
	// moved here with the list, which is the whole point of the re-home.
	await expect(page.locator('a[href="/projects/acme"]')).toContainText('2 warehouses');
});

test('gold warehouse and admin grant are optional — one create, no tuple', async ({ page }) => {
	await seed(page, { 'POST /v1/warehouses': created('solo-wh', 'solo', 'solo-bucket') });
	await page.goto('/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('solo');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('solo-wh');
	await dialog.getByLabel('Warehouse bucket').fill('solo-bucket');
	await dialog.getByLabel('Initial admin').clear();
	await dialog.getByRole('button', { name: 'Create project' }).click();
	// The toast marks the END of the flow, so everything read after it is settled — including "no tuple
	// ever fired", which is only a fact once the flow is over. It is also the ephemeral assertion
	// (sonner auto-dismisses), so it goes first and the durable ledger reads follow.
	await expect(page.getByText('Project solo created — warehouse solo-wh.')).toBeVisible();
	expect(await warehousePosts(page)).toEqual([
		{ id: 'solo-wh', project: 'solo', bucket: 'solo-bucket' },
	]);
	expect(await writtenTuples(page)).toEqual([]);
});

test('a denied first create toasts the failure and keeps the dialog open for a retry', async ({
	page,
}) => {
	await seed(page, { 'POST /v1/warehouses': { status: 403, body: { detail: 'forbidden' } } });
	await page.goto('/projects');
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
	// The failure lever is the seed itself (the write is server-side — page.route cannot reach it):
	// this test's bearer gets a 403 on the tuple write, nobody else's does.
	await seed(page, {
		'POST /v1/warehouses': created('acme-wh', 'acme', 'acme-wh'),
		'POST /v1/access/tuples': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/projects');
	await page.getByRole('button', { name: 'New project' }).click();
	const dialog = page.getByRole('dialog');
	await dialog.getByLabel('Project name').fill('acme');
	await dialog.getByLabel('Warehouse id', { exact: true }).fill('acme-wh');
	await dialog.getByRole('button', { name: 'Create project' }).click();
	await expect(
		page.getByText(/Project acme created with acme-wh, but admin grant for user:alice failed/),
	).toBeVisible();
	// …and the minted project is NOT reported as a success anywhere — a partial outcome that also
	// toasted "created — warehouse acme-wh, admin user:alice." would be the exact lie this pins.
	await expect(page.getByText(/^Project acme created — /)).toHaveCount(0);
});

test('the creation flow is invisible to a non-estate-admin (the /v1/me gate)', async ({ page }) => {
	await seed(page, { 'GET /v1/me': ME_MEMBER });
	await page.goto('/projects');
	// the gallery renders (bob's own membership) but the admin affordance never does
	await expect(page.locator('a[href="/projects/acme"]')).toBeVisible();
	await expect(page.getByRole('button', { name: 'New project' })).toHaveCount(0);
});
