import { expect, test } from '@playwright/test';
import { ME_ADMIN, ME_MEMBER, mockMe, signIn, TOKEN } from './session';

// The admin area's estate-admin door: `admin/+layout.ts` refuses with 403 before an admin page renders,
// on the `/management/v1/me` the console reads (here answered by `mockMe`, or by the mock catalog for the
// signed-in token). The door is presentation: the catalog and lineage enforce estate-admin on every read
// behind these pages. The sidebar is the other layer: its Operations rows are links for an estate admin
// and disabled rows naming the relation for anyone else.

// No shared beforeEach sign-in: each test signs in AS the identity it is about.

test('a non-estate-admin is refused every admin route, and the sidebar links none of them', async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.member });
	await mockMe(page, ME_MEMBER);
	// Every route the door covers, and it covers exactly the `admin` area now: #105 took the access
	// workbench and the audit trail to the console (`/settings/*`, behind ITS own layout gate) and
	// retired the tenants page, so `governance` is not a segment the console serves at all.
	for (const path of ['/admin/dlq', '/admin/streams', '/admin/events']) {
		await page.goto(path);
		await expect(
			page.getByRole('heading', { name: 'You don’t have access to Operations', level: 1 }),
		).toBeVisible();
		// The route's own content never renders behind the door.
		await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toHaveCount(0);
		await expect(page.getByRole('heading', { name: 'Streams' })).toHaveCount(0);
	}
	// The sidebar SHOWS the operations rows, disabled, and links none. Each says who may open it in
	// plain words, printed once under the group and read as every row's description, so a keyboard
	// and a touch reach the reason as a pointer does.
	const nav = page.getByRole('navigation', { name: 'Pages' });
	const reason = 'Only estate admins can open this.';
	for (const name of ['Events', 'Streams', 'DLQ']) {
		const row = nav.locator('[aria-disabled="true"]', { hasText: name });
		await expect(row).toBeVisible();
		await expect(row).toHaveAccessibleDescription(reason);
	}
	await expect(nav.getByText(reason)).toHaveCount(2); // Operations' note, and Settings' in the footer
	await expect(nav.getByText(reason).first()).toBeVisible();
	await expect(nav.locator('a[href^="/admin"]')).toHaveCount(0);
	// The catalog rows are no privilege: a non-admin gets them as links.
	await expect(nav.getByRole('link', { name: 'Tables', exact: true })).toBeVisible();
});

test('an estate admin passes the door and gets the operations rows as links', async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.admin });
	await mockMe(page, ME_ADMIN);
	await page.goto('/admin/dlq');
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toBeVisible();
	const nav = page.getByRole('navigation', { name: 'Pages' });
	for (const href of ['/admin/events', '/admin/streams', '/admin/dlq']) {
		await expect(nav.locator(`a[href="${href}"]`)).toBeVisible();
	}
	await expect(nav.locator('[aria-disabled="true"]')).toHaveCount(0);
	await expect(nav.locator('a[href="/admin/dlq"]')).toHaveAttribute('data-active', 'true');
});

test('a page has one name across its sidebar row, crumb, h1 and title; a named object is "<name> · <collection> · rask"', async ({
	context,
	page,
}) => {
	await signIn(context, { token: TOKEN.admin });
	await mockMe(page, ME_ADMIN);
	const nav = page.getByRole('navigation', { name: 'Pages' });
	const current = page
		.getByRole('navigation', { name: 'Breadcrumb' })
		.locator('[aria-current="page"]');
	for (const [path, name] of [
		['/', 'Home'],
		['/admin/events', 'Events'],
		['/admin/dlq', 'DLQ'],
		['/catalog/storage', 'Storage'],
		['/settings/access', 'Users & roles'],
		['/settings/audit', 'Audit'],
		['/workbench', 'Workbench'],
	] as const) {
		await page.goto(path);
		await expect(page, path).toHaveTitle(`${name} · rask`);
		await expect(nav.locator(`a[href="${path}"]`), path).toHaveText(name);
		await expect(current, path).toHaveText(name);
		await expect(page.getByRole('heading', { level: 1 }), path).toHaveText(name);
	}
	// `/admin` redirects to the Events page; its crumb is the group, named as the sidebar names it.
	await page.goto('/admin');
	await expect(page).toHaveURL(/\/admin\/events$/);
	await expect(page).toHaveTitle('Events · rask');
	const crumbs = page.getByRole('navigation', { name: 'Breadcrumb' });
	await expect(crumbs.getByText('Operations', { exact: true })).toBeVisible();
	await expect(crumbs.getByRole('link', { name: 'Operations' })).toHaveCount(0);
	for (const [path, title, h1] of [
		['/catalog/tables/db1%24t', 'db1$t · tables · rask', 'db1$t'],
		['/catalog/promotions/db1%24t', 'db1$t · promotions · rask', 'db1$t'],
	] as const) {
		await page.goto(path);
		await expect(page, path).toHaveTitle(title);
		await expect(page.getByRole('heading', { level: 1 }), path).toHaveText(h1);
	}
});

test('an unresolvable identity (catalog outage) fails CLOSED, never open', async ({
	context,
	page,
}) => {
	// The catalog's /management/v1/me 502s (TOKEN.down on the mock, and the route below for good
	// measure). fetchMe returns null for ANY failure, and null is a denial — never a default-open —
	// worded as the unconfirmed identity it is, the same refusal `/settings` gives.
	await signIn(context, { token: TOKEN.down });
	await page.route('**/api/catalog/management/v1/me', (route) =>
		route.fulfill({ status: 502, contentType: 'application/json', body: '{"detail":"down"}' }),
	);
	await page.goto('/admin/dlq');
	await expect(page.getByText('HTTP 403', { exact: true })).toBeVisible();
	await expect(page.getByText('could not be confirmed', { exact: false })).toBeVisible();
	await expect(page.getByRole('heading', { name: 'DLQ', exact: true })).toHaveCount(0);
});
