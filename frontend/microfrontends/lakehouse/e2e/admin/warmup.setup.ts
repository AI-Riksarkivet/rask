import { test } from '@playwright/test';
import { signIn } from './session';

// Compile-warm the AUTH-ON dev server before the parallel admin suite (see playwright.config.ts
// projects comment). The admin area is a SECOND dev server and a second Vite compile, so it gets
// nothing from the auth-off warmup — without this the first admin spec to run pays the whole cold
// compile of the heaviest route it drives (`/lakehouse/catalog/tables/db1$t`, Svelte Flow +
// apache-arrow) and blows the 30s test timeout, deterministically, whenever the CSS or a component
// under it changed. Signed in, because the
// login-first gate redirects a signed-out navigation to /auth/login — a home-zone route that does not
// exist on this server, so an anonymous goto would never compile the page it was meant to warm.
test('warm the admin dev server routes', async ({ context, page }) => {
	test.setTimeout(180_000);
	await signIn(context);
	for (const path of [
		'/lakehouse/admin',
		// No `/lakehouse/governance/*` and no `/lakehouse/admin/tenants` rows: #105 moved the access
		// workbench and the audit trail to the home zone's `/settings/*` and retired the tenants page
		// (`/projects` IS the estate's project list). Their warm-ups moved with their specs.
		'/lakehouse/admin/events',
		'/lakehouse/admin/streams',
		'/lakehouse/admin/dlq',
		// No `/lakehouse/catalog/projects*` rows: both routes are DELETED (2026-08-03 ruling — a
		// project is the top of the hierarchy, so its list and its overview are the home zone's).
		// Warming a route the zone no longer serves compiles nothing and costs a 404 round-trip.
		'/lakehouse/catalog/warehouses',
		'/lakehouse/catalog/warehouses/acme-wh',
		'/lakehouse/catalog/namespaces/acme-silver',
		'/lakehouse/catalog/namespaces',
		// The heaviest route in the zone (Svelte Flow + apache-arrow): the per-object access specs
		// drive it, and on a cold compile the first of them times out at 30s every time.
		'/lakehouse/catalog/tables/db1%24t',
	]) {
		await page.goto(path).catch(() => {});
	}
});
