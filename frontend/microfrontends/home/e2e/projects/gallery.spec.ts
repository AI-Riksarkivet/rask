import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, ME_MEMBER, signIn, TOKEN } from './session';
import { seed as seedFor } from './mock-client';

// The gallery's DEGRADE contract — the behaviour `$lib/gallery.ts` exists for and that nothing tested
// anywhere before this file. Memberships come from the frozen `/v1/me`; an estate admin ALSO gets the
// estate-wide listing through this zone's `/capi/v1/projects` pass-through; and when that listing
// fails, the caller still sees their own projects. "Degrade, never 500" is only a claim until a run
// proves the page still answers 200 with the right rows on the failing path.
//
// EVERY mount here is `/projects`. `/` used to render this same gallery, which is why some of these
// read the landing; the two-level ruling made `/` the estate's HOME (an insights scaffold) and left
// `/projects` as the list's only surface — so the membership empty state and the degrade paths
// belong here, where someone actually asks "where are my projects".

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

/** An estate admin who is also a member of two projects — memberships and the estate listing are
 *  different reads, and the fallback is exactly "the second one failed, show the first". */
const ME_ADMIN_WITH_MEMBERSHIPS = {
	...ME_ADMIN,
	projects: [
		{ project: 'acme', role: 'admin' as const },
		{ project: 'beta', role: 'member' as const },
	],
};

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('a 403 on the estate listing degrades to the caller’s own memberships — never a 500', async ({
	page,
}) => {
	await seed(page, {
		'GET /v1/me': ME_ADMIN_WITH_MEMBERSHIPS,
		// estate_admin is `can_observe_events`; the estate LISTING is gated separately, so an identity
		// can hold the first and be refused the second. That is the fallback's real trigger.
		'GET /v1/projects': { status: 403, body: { detail: 'forbidden' } },
	});
	const res = await page.goto('/projects');
	// The failing upstream must not reach the user as a 500 or an error boundary…
	expect(res?.status()).toBe(200);
	await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
	// …and the rows are the membership-derived ones: role badges present, warehouse counts absent
	// (the listing is the only thing that knows them, and it refused).
	const acme = page.locator('a[href="/projects/acme"]');
	await expect(acme).toContainText('acme');
	await expect(acme).toContainText('admin');
	await expect(acme).not.toContainText('warehouse');
	const beta = page.locator('a[href="/projects/beta"]');
	await expect(beta).toContainText('beta');
	await expect(beta).toContainText('member');
});

test('an unreachable catalog degrades the same way — the memberships /v1/me already resolved', async ({
	page,
}) => {
	// No seed for `GET /v1/projects` at all → the mock answers 404, standing in for any non-ok listing.
	// The load's `res.ok` branch and its `catch` are the same fallback, and both must reach the user as
	// their projects rather than as an error.
	await seed(page, { 'GET /v1/me': ME_ADMIN_WITH_MEMBERSHIPS });
	const res = await page.goto('/projects');
	expect(res?.status()).toBe(200);
	await expect(page.locator('a[href="/projects/acme"]')).toContainText('admin');
	await expect(page.locator('a[href="/projects/beta"]')).toContainText('member');
});

test('a non-estate-admin never reads the estate listing at all', async ({ page }) => {
	// Seeded with a tenant bob is NOT a member of. If the listing were consulted for a non-estate-admin
	// it would leak straight onto his gallery, so its ABSENCE is the assertion.
	await seed(page, {
		'GET /v1/me': ME_MEMBER,
		'GET /v1/projects': [{ project: 'tenant-b', warehouses: [{ id: 'tenant-b-wh' }] }],
	});
	await page.goto('/projects');
	await expect(page.locator('a[href="/projects/acme"]')).toContainText('member');
	await expect(page.locator('a[href="/projects/tenant-b"]')).toHaveCount(0);
});

test('/projects mounts the same gallery as / — one implementation, two surfaces', async ({
	page,
}) => {
	await seed(page, {
		'GET /v1/me': ME_ADMIN,
		'GET /v1/projects': [
			{ project: 'acme', warehouses: [{ id: 'acme-wh' }, { id: 'acme-gold' }] },
			{ project: 'beta', warehouses: [{ id: 'beta-wh' }] },
		],
	});
	await page.goto('/projects');
	// Only the heading differs between the two mounts (`ProjectGallery`'s one prop).
	await expect(page.getByRole('heading', { name: 'Projects', exact: true })).toBeVisible();
	await expect(page.locator('a[href="/projects/acme"]')).toContainText('2 warehouses');
	// Singular/plural is the component's, not the route's — a second copy of this list is exactly what
	// the ruling deletes, so both counts are read on the surface that is supposed to be shared.
	await expect(page.locator('a[href="/projects/beta"]')).toContainText('1 warehouse');
	// The estate listing supplies no role for a project the admin is not a member of, so no badge.
	await expect(page.locator('a[href="/projects/acme"]')).not.toContainText('admin');
});
