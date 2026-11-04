import type { Page } from '@playwright/test';
import { MOCK_CATALOG } from './ports';

// The test-side half of `mock-catalog.ts`: seed exact catalog answers for ONE bearer, and read back
// what that bearer's app server actually sent. Shared by all three project specs because all three
// drive the same mock through the same two endpoints — three private copies of these six lines is how
// the lakehouse suite ended up with the same helper written three slightly different ways.

/** One mutating request the app server made, as the mock recorded it. */
export type MockCall = { method: string; path: string; body: unknown };

/** Pre-seed exact catalog responses for a bearer ("METHOD /path" → body, or `{status, body}`). */
export async function seed(
	page: Page,
	token: string,
	routes: Record<string, unknown>,
): Promise<void> {
	await page.request.post(`${MOCK_CATALOG}/__mock/seed`, { data: { bearer: token, routes } });
}

/** Every mutating request the mock recorded for this bearer, in order. */
export async function calls(page: Page, token: string): Promise<MockCall[]> {
	const res = await page.request.get(`${MOCK_CATALOG}/__mock/calls`, {
		headers: { authorization: `Bearer ${token}` },
	});
	return ((await res.json()) as { calls: MockCall[] }).calls;
}

/** The bodies this bearer POSTed to one path — the wire shape a create/grant actually put on it. */
export async function postsTo(page: Page, token: string, path: string): Promise<unknown[]> {
	return (await calls(page, token))
		.filter((c) => c.method === 'POST' && c.path === path)
		.map((c) => c.body);
}
