import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from './session';
import { seed as seedFor } from './mock-client';

// `/settings` is an ESTATE page — the third place in the main menu — but three of its rows open
// surfaces the LAKEHOUSE app still serves. That is a hop between two SvelteKit apps, and it has two
// failure modes that look nothing alike:
//
//   1. SILENT. A soft client-side navigation into `/lakehouse/...` targets a route the home zone's
//      manifest does not contain. `data-sveltekit-reload` is the fix, and the STATIC gate is what
//      enforces it — `@rask/zone-contract`'s cross-zone-reload walks Svelte's AST, and it was blind
//      here until the page stopped rendering the hrefs from a loop (a `{…}` href reads as an opaque
//      expression, so those rows were invisible to it and the attribute was correct only because a
//      human remembered). The first test below is the same assertion against the rendered DOM.
//
//      A browser CANNOT stand in for that gate, and the honest version of this file says so: without
//      the attribute SvelteKit still ends up doing a full navigation (`server_fallback` →
//      `native_navigation`), so "the document reloaded" is true either way. The measurable cost of
//      the missing attribute is a phantom client-side 404 attributed to this zone — see the second
//      test, which asserts exactly that and nothing more.
//   2. DISORIENTING. Even when the hop works, the navbar swaps from the main menu to the lakehouse's
//      own bar mid-task, and the user never asked to change apps. So the row must SAY where it goes
//      before it is clicked, in the accessible name and not only in pixels.
//
// The unwired half gets the mirror-image assertion: those rows must read as decisions, not as
// controls that stopped working.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

/** Links inside the settings page's own sections — never the shell's navbar or sidebar, which other
 *  specs own and whose links are not this page's claim. */
const rows = (page: Page) => page.locator('section[aria-labelledby^="settings-"] a[href]');

/** The zone base prefixes, and the APP NAME each one is called in the UI. Mirrors `ZONES` in
 *  `@rask/zone-contract` and the zone labels in `@rask/ui`'s nav-config (`explorer` is "Explorer",
 *  `annotator` is "Annotate"); a link whose first segment is one of these leaves the home app.
 *
 *  A map rather than a list because the badge assertion below is DERIVED from it. Hardcoding
 *  "Opens in Lakehouse" for every cross-zone link read fine while all three rows happened to go to
 *  the lakehouse, and was a trap: it would have failed a correctly-labelled `/explorer` row and
 *  passed a mislabelled one, i.e. rewarded exactly the wrong answer. */
const ZONE_APP: Record<string, string> = {
	lakehouse: 'Lakehouse',
	explorer: 'Explorer',
	annotator: 'Annotate',
	compute: 'Compute',
	studio: 'Studio',
	train: 'Train',
};

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('every settings link that leaves the estate hard-navigates AND says so', async ({ page }) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	await page.goto('/settings');
	await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();

	// Read EVERY link on the page rather than the three we expect: the assertion that matters is about
	// the class of link, so a fourth row added later without the attribute must fail here.
	const links = await rows(page).evaluateAll((els) =>
		els.map((el) => ({
			href: el.getAttribute('href') ?? '',
			reload: el.hasAttribute('data-sveltekit-reload'),
			text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
		})),
	);
	expect(links.length).toBeGreaterThan(0);

	const crossZone = links.filter((l) => (l.href.split('/')[1] ?? '') in ZONE_APP);
	expect(crossZone.map((l) => l.href).sort()).toEqual([
		'/lakehouse/admin/tenants',
		'/lakehouse/governance/access',
		'/lakehouse/governance/audit',
	]);

	for (const link of crossZone) {
		// The attribute, on every one of them. This is the assertion that actually GATES the attribute
		// — see the note on the next test for why the browser cannot.
		expect(link.reload, `${link.href} must carry data-sveltekit-reload`).toBe(true);
		// …and the DESTINATION named inside the link, so it lands in the accessible name and is
		// announced, not merely drawn as a badge someone might not read. Derived from the href, so the
		// row and the label it makes cannot disagree.
		const app = ZONE_APP[link.href.split('/')[1] ?? ''];
		expect(link.text, `${link.href} must name the app it opens`).toContain(`Opens in ${app}`);
	}

	// The rows are still identifiable by name, and the estate-scoped three are exactly these.
	for (const name of ['Access', 'Tenants', 'Audit']) {
		await expect(page.getByRole('link', { name: new RegExp(`^${name}\\b`) })).toBeVisible();
	}
});

test('the Access row leaves the app as a document load, and reports no phantom 404 on the way out', async ({
	page,
}) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	await page.goto('/settings');

	// TWO assertions, and only the second one discriminates. That is worth stating, because the obvious
	// version of this test does not work and looks like it does.
	//
	// The sentinel below is a window property: it survives a client-side (soft) navigation and cannot
	// survive a document load. It is NOT evidence that `data-sveltekit-reload` is present. Removing the
	// attribute and re-running proved it — the test still passed. SvelteKit's client router intercepts
	// the click, finds no route for `/lakehouse/…` in home's manifest, and ends `navigate()` in
	// `server_fallback` → `native_navigation`, i.e. `location.href = url`
	// (`@sveltejs/kit@2.70.1`, src/runtime/client/client.js: the "reload the page we are already on"
	// guard does not apply, so the 404 branch falls straight through to a real navigation). The document
	// reloads either way. So does the URL, and so does the server hit.
	//
	// What actually differs is the route the browser takes to get there: without the attribute the
	// router first raises a 404 through `hooks.client.ts`, and this zone's `handleError` logs it as a
	// `[zone-error]` line — a fault attributed to `home`, in the estate's error telemetry, for a link
	// that works. Silence on that channel is the observable difference, and it is what this asserts.
	//
	// Captured into sessionStorage rather than off `page.on('console')`, and that is load-bearing too.
	// The console version RACES: `handleError` logs and then the very next statement replaces the
	// document, so CDP sometimes tears the context down before delivering the event — measured as a
	// playwright "flaky" (fail, then pass on retry), which with `retries: 1` is a GREEN run. A gate that
	// reports a real regression half the time is not a gate. The wrapper below writes synchronously,
	// inside the same call, and sessionStorage survives a same-origin document load — so by the time the
	// destination has rendered the evidence is already stored.
	await page.evaluate(() => {
		sessionStorage.removeItem('__zoneErrors');
		const original = console.error.bind(console);
		console.error = (...args: unknown[]) => {
			const line = String(args[0] ?? '');
			if (line.startsWith('[zone-error]')) {
				sessionStorage.setItem(
					'__zoneErrors',
					`${sessionStorage.getItem('__zoneErrors') ?? ''}${line}\n`,
				);
			}
			original(...args);
		};
		Object.assign(window, { __sameDocument: true });
	});

	await Promise.all([
		page.waitForURL('**/lakehouse/governance/access'),
		page.getByRole('link', { name: /^Access\b/ }).click(),
	]);

	// The hop is a real document navigation — in the deployed estate that is what reaches the ingress,
	// which is the only party that can hand the path to the lakehouse app.
	expect(await page.evaluate(() => '__sameDocument' in window)).toBe(false);
	// …and the home zone did not blame itself for it on the way.
	const zoneErrors = await page.evaluate(() => sessionStorage.getItem('__zoneErrors'));
	expect(zoneErrors, 'the hop must not raise a client-side error in the home zone').toBeNull();
});

test('the unwired rows read as decisions, not as broken controls', async ({ page }) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	await page.goto('/settings');

	for (const title of ['Notifications', 'New-project defaults', 'Credentials']) {
		const card = page.locator('[data-slot="card"]').filter({ hasText: title });
		await expect(card).toHaveCount(1);
		// Named as unbuilt…
		await expect(card.getByText('Not wired yet')).toBeVisible();
		// …with the specific missing thing that blocks it. "Blocked on <x>" is a decision someone made;
		// a bare disabled row is indistinguishable from a bug.
		await expect(card.getByText(/^Blocked on .+\.$/)).toBeVisible();
		// …and nothing to click. A control that exists and does nothing is the failure mode this page
		// is trying not to have; the honest shape is no control at all.
		await expect(card.locator('a, button, input, select, textarea')).toHaveCount(0);
	}
});
