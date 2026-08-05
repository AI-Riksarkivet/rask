import { test } from '@playwright/test';
import { signIn } from '../session';

// Compile-warm the AUTH-ON dev server before the parallel projects suite. That is a SECOND dev server
// and a second Vite compile, so it gets nothing from the auth-off warmup — without this the first spec
// to reach `/projects` pays the whole cold compile and blows the 30s test timeout, deterministically,
// whenever anything under it changed.
//
// Signed in, because the login-first gate redirects a signed-out page navigation to /auth/login: an
// anonymous goto would compile the login route instead of the page it was meant to warm. The bearer is
// unseeded on the mock catalog, so `/v1/me` answers 401 and the pages render their degraded states —
// which is all a warmup needs: `goto` resolving means Vite compiled the route.
test('warm the auth-ON dev server routes', async ({ context, page }) => {
	test.setTimeout(180_000);
	await signIn(context);
	for (const path of ['/', '/projects', '/projects/acme']) {
		await page.goto(path).catch(() => {});
	}
});
