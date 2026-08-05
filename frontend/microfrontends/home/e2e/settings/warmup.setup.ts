import { test } from '@playwright/test';
import { signIn, ME_ADMIN, TOKEN } from '../session';
import { seed } from '../mock-client';

// Compile-warm the auth-ON dev server's SETTINGS routes before the parallel suite. `/settings/access`
// pulls in Svelte Flow and `/settings/audit` the whole TanStack data-table; on a cold Vite compile the
// first spec to touch either pays the full cost and blows the 30s per-test timeout, deterministically,
// whenever a component under them changed. The lakehouse suite carried the same warm-up for the same
// two pages before #105 moved them here.
//
// Signed in, because the settings layout gate 404s an unresolved identity and SvelteKit does not
// compile the page behind a load that threw — an anonymous goto would warm nothing.
test('warm the settings routes', async ({ context, page }) => {
	test.setTimeout(180_000);
	const token = `${TOKEN.admin}:warmup`;
	await signIn(context, { token });
	await seed(page, token, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	for (const path of ['/settings', '/settings/access', '/settings/audit']) {
		await page.goto(path).catch(() => {});
	}
});
