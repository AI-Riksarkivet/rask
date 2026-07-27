import { test } from '@playwright/test';
import { signIn } from './session';

// Compile-warm the AUTH-ON dev server before the parallel admin suite (see playwright.config.ts
// projects comment). The admin area is a SECOND dev server and a second Vite compile, so it gets
// nothing from the auth-off warmup — without this the first admin spec to run pays the whole cold
// compile of `/lakehouse/admin/access` (which pulls in Svelte Flow) and blows the 30s test timeout,
// deterministically, whenever the CSS or a component under it changed. Signed in, because the
// login-first gate redirects a signed-out navigation to /auth/login — a home-zone route that does not
// exist on this server, so an anonymous goto would never compile the page it was meant to warm.
test('warm the admin dev server routes', async ({ context, page }) => {
	test.setTimeout(180_000);
	await signIn(context);
	for (const path of [
		'/lakehouse/admin',
		'/lakehouse/admin/access',
		'/lakehouse/admin/tenants',
		'/lakehouse/admin/audit',
		'/lakehouse/admin/events',
		'/lakehouse/admin/streams',
		'/lakehouse/admin/dlq',
	]) {
		await page.goto(path).catch(() => {});
	}
});
