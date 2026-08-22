import { test } from '@playwright/test';
import { signIn } from './session';

// Compile-warm the dev server before the parallel suite (see playwright.config.ts projects comment).
// No mocks: an unseeded upstream read is irrelevant here — `goto` resolving is what proves Vite compiled
// the route. SIGNED IN, because this server runs auth-ON and the login-first gate redirects an anonymous
// navigation to `/auth/login` (a home-zone route, absent here), so an anonymous goto would compile the
// 404 rather than the page it meant to warm.
test('warm the dev server routes', async ({ context, page }) => {
	test.setTimeout(180_000);
	await signIn(context);
	for (const path of ['/models', '/models/runs', '/models/experiments']) {
		await page.goto(path).catch(() => {});
	}
});
