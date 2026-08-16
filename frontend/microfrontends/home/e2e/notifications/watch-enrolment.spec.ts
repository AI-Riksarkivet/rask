import { test, expect, type Page } from '@playwright/test';
import { ME_MEMBER, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// WATCH ENROLMENT — the only door into the estate's second targeting source, and it was shut.
//
// The page used to load its project list from `getProjects()`, the `/capi/v1/projects` pass-through,
// which needs the controlplane to reach the Kubernetes API and read `platform.rask.io` Project CRs.
// That CRD is not installed on this estate and is not in this repo — verified live: `kubectl get crd`
// returns 71 CRDs and none of them is it, and an in-cluster `GET /api/projects/` answers
// `503 {"detail":"cannot reach kubernetes api"}`.
//
// The failure was not merely "no projects". `onMount` awaited `Promise.all([readWatches(),
// getProjects()])`, so the 503 rejected the whole thing, `watches` stayed `null`, and the page
// rendered "Watching is unavailable on this stack… the notification service is not reachable" — an
// outage in the CONTROLPLANE reported as an outage in NOTIFICATIONS, on the one surface a person uses
// to turn watching on. Nobody could enrol, and the message pointed at the wrong service.
//
// Memberships now come from `me.projects`, the frozen `/v1/me` contract the layout already resolves
// for every zone, which is the same source `$lib/gallery.ts` reads. These tests exercise the page with
// NO controlplane answer of any kind seeded — the mock catalog 404s anything unstubbed — so a
// regression that reintroduces the dependency fails here rather than in a cluster.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> => seedFor(page, token, routes);

const ME_WITH_MEMBERSHIPS = {
	...ME_MEMBER,
	projects: [
		{ project: 'acme', role: 'member' as const },
		{ project: 'beta', role: 'admin' as const },
	],
};

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.member}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('a member can see their projects to watch while the controlplane is unreachable', async ({ page }) => {
	// Only `/v1/me` is seeded. `/v1/projects` and every controlplane path stay unstubbed and 404 —
	// standing in for the 503 the live estate returns. If the page still consults them, it breaks.
	await seed(page, { 'GET /v1/me': ME_WITH_MEMBERSHIPS });
	const res = await page.goto('/notifications');

	expect(res?.status()).toBe(200);
	await expect(page.locator('[data-slot="watch-row"]')).toHaveCount(2);
	await expect(page.locator('[data-project="acme"]')).toBeVisible();
	await expect(page.locator('[data-project="beta"]')).toBeVisible();
	// The precise regression: the un-wired card blamed notifications for the controlplane's fault.
	await expect(page.getByText('Watching is unavailable on this stack')).toBeHidden();
});

test('a member of nothing is told that membership is what makes a project watchable', async ({ page }) => {
	await seed(page, { 'GET /v1/me': { ...ME_MEMBER, projects: [] } });
	await page.goto('/notifications');

	await expect(page.getByText('You are not a member of any project yet.')).toBeVisible();
	await expect(page.locator('[data-slot="watch-row"]')).toHaveCount(0);
});

test('a session whose identity cannot be resolved is NOT told to sign in', async ({ page }) => {
	// The third state, taken from `gallery.ts`. `me` is null for BOTH "signed out" and "signed in but
	// /v1/me failed", and those are opposite situations: one is fixed by signing in and the other
	// cannot be. Sending this user round the sign-in loop is the failure being prevented.
	await seed(page, { 'GET /v1/me': { status: 503, body: { detail: 'catalog unavailable' } } });
	await page.goto('/notifications');

	await expect(page.locator('[data-slot="identity-unavailable"]')).toBeVisible();
	await expect(page.getByText('We could not confirm who you are.')).toBeVisible();
	await expect(page.getByText('Signing in again will not help', { exact: false })).toBeVisible();
});
