import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { postsTo, seed as seedFor } from '../mock-client';

// Screenshots are EVIDENCE the owner reviews, not assertions — the same convention
// `settings/gated-action.spec.ts` established. The maintenance surface earns them because its
// whole job is making a winner-takes-all resolution legible: a reviewer has to be able to SEE
// that the project's own knobs and the tiers shadowing it are rendered as different things.
const SHOTS = 'e2e/__shots__';

// `/projects/<p>` § Maintenance (#65) — the project-scoped VIEW of the maintenance policies governing
// a tenant's data. The set/describe/delete trio for a project record shipped with #84 and had no
// surface at all; the LIST is what makes a surface possible, because a page cannot describe records
// whose ids it would have to already know.
//
// Three things are pinned here, and each is a way the page could be green and wrong:
//
//  1. the ZONE SEAM — every record row links DOWN a rung into the lakehouse and must carry
//     `data-sveltekit-reload`, or SvelteKit soft-navigates into a route home does not own and 404s.
//     `@rask/zone-contract`'s static gate cannot see this direction (its ZONES list excludes home).
//  2. the SHORT-LIST banner — `incomplete` means a namespace binding could not be read, so policies
//     under it are missing. Swallowed, the list reads as checked-and-clean.
//  3. the SET body — an omitted knob means INHERIT. `put_policy` overwrites the whole record, so a
//     form that posted its model's defaults for untouched fields would silently clear the compaction
//     memory bound and flip version-reclamation ownership.

const DETAIL = {
	project: 'acme',
	warehouses: [{ id: 'acme-wh', bucket: 'acme-bucket', status: 'active' }],
	admins: ['user:alice'],
};

/** The listing as the catalog answers it: the project's own record plus one record from each lower
 *  tier — the tiers that SHADOW it. */
const POLICIES = {
	project: 'acme',
	buckets: ['acme-bucket'],
	namespaces: ['gold'],
	incomplete: false,
	skipped_bindings: [],
	policies: [
		{
			kind: 'namespace',
			id: 'gold',
			path: 'lance-catalog/gold',
			retention_days: 30,
			compact_enabled: true,
			cleanup_enabled: true,
			optimize_indices_enabled: true,
		},
		{
			kind: 'project',
			id: 'acme',
			path: '',
			buckets: ['acme-bucket'],
			retention_days: 90,
			compact_enabled: true,
			cleanup_enabled: true,
			optimize_indices_enabled: true,
		},
		{
			kind: 'table',
			id: 'gold$pages',
			path: 'acme-bucket/u1_gold$pages',
			compact_enabled: false,
			cleanup_enabled: false,
			optimize_indices_enabled: false,
		},
	],
};

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

test.beforeEach(async ({ context, page }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects/acme': DETAIL });
});

test('renders the project record and every record that shadows it', async ({ page }) => {
	await seed(page, { 'GET /v1/projects/acme/policies': POLICIES });
	await page.goto('/projects/acme');

	// The project's OWN tier — the one this page can edit — shown as its knobs, not as an "effective"
	// merge. Resolution is winner-takes-all, so a merged view would be a lie.
	await expect(page.getByRole('heading', { name: 'Maintenance' })).toBeVisible();
	await expect(page.getByText('retention 90d', { exact: true })).toBeVisible();
	await expect(page.getByRole('button', { name: 'Edit' })).toBeVisible();

	// …and the tiers below it, read-only, with the record's own physical path.
	const ns = page.getByRole('row').filter({ hasText: 'gold$pages' });
	await expect(ns).toContainText('acme-bucket/u1_gold$pages');
	await expect(ns).toContainText('off'); // compact_enabled:false is a STATE, not an absence

	// SCROLL FIRST, and shoot the SECTION rather than the page. `fullPage: true` produced a shot of
	// the Hierarchy block with Maintenance below the fold: the AppShell scrolls an inner container,
	// so the page itself is viewport-height and "full page" means the viewport.
	const maint = page.getByRole('heading', { name: 'Maintenance' });
	await maint.scrollIntoViewIfNeeded();
	await page.screenshot({ path: `${SHOTS}/65-policies-project.png` });
});

test('each record row crosses INTO the lakehouse zone with data-sveltekit-reload', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/projects/acme/policies': POLICIES });
	await page.goto('/projects/acme');
	// A namespace record links to the namespace rung, a table record to the table rung — the lakehouse
	// owns both, and this zone's route manifest owns neither.
	for (const href of [
		'/lakehouse/catalog/namespaces/gold',
		'/lakehouse/catalog/tables/gold%24pages',
	]) {
		const link = page.locator(`a[href="${href}"]`);
		await expect(link).toBeVisible();
		await expect(link).toHaveAttribute('data-sveltekit-reload', '');
	}
});

test('an unreadable binding is announced as a short list, never swallowed', async ({ page }) => {
	await seed(page, {
		'GET /v1/projects/acme/policies': {
			...POLICIES,
			policies: [],
			namespaces: [],
			incomplete: true,
			skipped_bindings: ['file:///control/_warehouses/bindings/zzz.json'],
		},
	});
	await page.goto('/projects/acme');
	// The whole point of the flag: an empty list that MIGHT be short must not read as "checked and
	// clean" — the same failure class as the reconciler's `unbound_namespaces` detector.
	await expect(page.getByText(/This list may be short/)).toBeVisible();
	await expect(page.getByText(/1 namespace binding record could not be read/)).toBeVisible();
});

test('a denied listing is named, never rendered as “no policies”', async ({ page }) => {
	await seed(page, {
		'GET /v1/projects/acme/policies': { status: 403, body: { detail: 'forbidden' } },
	});
	await page.goto('/projects/acme');
	await expect(
		page.getByText('Denied: maintenance policies need project-admin access.'),
	).toBeVisible();
	// A 403 must not offer the affordance that would overwrite the record it could not read.
	await expect(page.getByRole('button', { name: 'Set policy' })).toHaveCount(0);
});

test('Save posts ONLY the knobs the form touched — an omitted field means inherit', async ({
	page,
}) => {
	await seed(page, {
		'GET /v1/projects/acme/policies': { ...POLICIES, policies: [] },
		'POST /v1/project/acme/policy/set': {
			kind: 'project',
			id: 'acme',
			path: '',
			buckets: ['acme-bucket'],
			retention_days: 45,
			compact_enabled: true,
			cleanup_enabled: true,
			optimize_indices_enabled: true,
		},
	});
	await page.goto('/projects/acme');
	await page.getByRole('button', { name: 'Set policy' }).click();
	await page.getByLabel('retention days').fill('45');
	await page.getByRole('button', { name: 'Save policy' }).click();

	await expect
		.poll(async () => (await postsTo(page, token, '/v1/project/acme/policy/set')).length)
		.toBe(1);
	const [body] = (await postsTo(page, token, '/v1/project/acme/policy/set')) as [
		Record<string, unknown>,
	];
	expect(body.retention_days).toBe(45);
	// The three step flags follow the one `enabled` switch — "maintenance on this project", not
	// "compaction only"; cleanup DELETES old versions, so it must never be left on under an off switch.
	expect(body.compact_enabled).toBe(true);
	expect(body.cleanup_enabled).toBe(true);
	expect(body.optimize_indices_enabled).toBe(true);
	// The decisive half: the untouched knobs are ABSENT, not null. `put_policy` overwrites the whole
	// record and the catalog only carries forward what the caller did not SET, so a null here would
	// erase a value that was governing real data.
	for (const absent of [
		'retain_versions',
		'compact_interval_hours',
		'target_rows_per_fragment',
		'scan_batch_size',
		'auto_cleanup_interval_commits',
	])
		expect(body).not.toHaveProperty(absent);
});
