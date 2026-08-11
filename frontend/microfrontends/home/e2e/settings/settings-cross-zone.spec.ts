import { test, expect, type Page } from '@playwright/test';
import { ME_ADMIN, signIn, TOKEN } from '../session';
import { seed as seedFor } from '../mock-client';

// `/settings` used to be an ESTATE page whose three rows opened surfaces the LAKEHOUSE app served —
// a hop between two SvelteKit apps, with two failure modes that look nothing alike (a soft nav into a
// route this zone's manifest does not contain, and a navbar that swaps apps mid-task without saying
// so). This file was the browser half of that contract.
//
// #105 removed the hop instead of documenting it. `/settings/access` and `/settings/audit` are routes
// of THIS app now, and `/projects` always was, so the contract inverts: NO row of the settings page
// leaves the estate, every one of them is a soft navigation, and none of them carries
// `data-sveltekit-reload`.
//
// The file stays, as the INVERSE assertion, and that is deliberate. `@rask/zone-contract`'s
// cross-zone-reload gate only ever flags a MISSING attribute — it cannot flag a row that quietly
// starts pointing at another app again, because such a row would simply need the attribute and would
// have it. The claim worth holding is that the platform surfaces are served here; the day one of them
// moves back out, this fails and someone has to argue for it rather than discover it in a navbar that
// changed shape mid-click.
//
// The unwired half keeps the mirror-image assertion: those rows must read as decisions, not as
// controls that stopped working.

let token: string;

const seed = (page: Page, routes: Record<string, unknown>): Promise<void> =>
	seedFor(page, token, routes);

/** The settings page's own sections — never the shell's navbar or sidebar, which other specs own and
 *  whose links are not this page's claim. */
const section = (page: Page) => page.locator('section[aria-labelledby^="settings-"]');
/** Every link inside them. */
const rows = (page: Page) => section(page).locator('a[href]');

/**
 * Wait until the client router has taken over.
 *
 * Load-bearing for the soft-navigation test below, and ONLY since the port: before hydration an `<a>`
 * click is a plain browser navigation, so the document reloads and the sentinel dies — which is what
 * this test used to ASSERT, so the race was invisible. Now that the correct answer is "the document
 * survives", an un-hydrated click fails, and it failed intermittently until this gate landed.
 *
 * The navbar's panel is client-only markup — the SSR HTML contains no
 * `navigation-menu-viewport` at all — so its appearance is proof the app is live. Retried, because the
 * click itself is what races (the estate's existing idiom: lakehouse's `openPanel`).
 */
async function hydrated(page: Page): Promise<void> {
	const trigger = page
		.getByRole('navigation', { name: 'Zones' })
		.getByRole('button', { name: 'Settings' });
	const panel = page.locator('[data-slot="navigation-menu-viewport"]');
	await expect(async () => {
		await trigger.click();
		await expect(panel).toHaveCount(1, { timeout: 1000 });
	}).toPass({ timeout: 20_000 });
	await page.keyboard.press('Escape');
	await expect(panel).toHaveCount(0);
}

/** The zone base prefixes — a link whose first segment is one of these leaves the home app. Mirrors
 *  `ZONES` in `@rask/zone-contract`. `projects` and `settings` are deliberately NOT here: they are
 *  home's own routes (the shell's `HOME_ROUTES`), which is exactly what makes these rows same-zone. */
const ZONE_BASES = ['lakehouse', 'explorer', 'annotator', 'compute', 'studio', 'train'];

test.beforeEach(async ({ context }, testInfo) => {
	token = `${TOKEN.admin}:${testInfo.testId}`;
	await signIn(context, { token });
});

test('no settings row leaves the estate — every one is served by this app', async ({ page }) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	await page.goto('/settings');
	await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible();

	// Read EVERY link on the page rather than the three we expect: the assertion is about the CLASS of
	// link, so a fourth row added later that points into another app must fail here.
	const links = await rows(page).evaluateAll((els) =>
		els.map((el) => ({
			href: el.getAttribute('href') ?? '',
			reload: el.hasAttribute('data-sveltekit-reload'),
			text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
		})),
	);
	expect(links.length).toBeGreaterThan(0);

	// The exact set, in full: pinning the hrefs is what stops "no cross-zone links" from passing
	// vacuously on a page that lost its rows.
	expect(links.map((l) => l.href).sort()).toEqual([
		'/projects',
		'/settings/access',
		'/settings/audit',
		'/settings/notifications',
	]);
	expect(links.filter((l) => ZONE_BASES.includes(l.href.split('/')[1] ?? ''))).toEqual([]);

	// …and no row carries the hard-nav attribute, because none of them needs it. It is not free: the
	// attribute forces a document load, which is precisely the cost the port removed.
	for (const link of links) {
		expect(link.reload, `${link.href} is same-zone and must NOT hard-navigate`).toBe(false);
		// Nor may a row announce another app. The badge said "Opens in Lakehouse" and landed in the
		// link's accessible name; a row that keeps saying so after the page moved here is a lie a
		// screen reader delivers first.
		expect(link.text, `${link.href} must not announce another app`).not.toMatch(/Opens in /);
	}

	// The rows are still identifiable by name, and the platform three are exactly these. Scoped to the
	// sections: the navbar carries a "Projects" link too now (it is a main-menu entry AND a settings
	// row), so an unscoped lookup matches two elements — and this file's claim is about the page.
	for (const name of ['Users & roles', 'Projects', 'Audit']) {
		await expect(
			section(page).getByRole('link', { name: new RegExp(`^${name}\\b`) }),
		).toBeVisible();
	}
});

test('the Users & roles row is a SOFT navigation — the document survives it', async ({ page }) => {
	await seed(page, {
		'GET /v1/me': ME_ADMIN,
		'GET /v1/projects': [],
		'GET /v1/access/model': { dsl: 'type user\n', authorization_model_id: '01MODEL' },
		'GET /v1/access/tuples': { tuples: [], continuation: null },
		'GET /v1/table': { tables: [] },
	});
	await page.goto('/settings');
	await hydrated(page);

	// The sentinel is a window property: it survives a client-side (soft) navigation and cannot survive
	// a document load. That polarity is the whole test. It used to run the other way round — this row
	// left the app, and the honest assertion then was that the sentinel was GONE. The port is what
	// flipped it, and a regression to a cross-app row flips it back.
	//
	// It also catches the cheap mistake the port could have made: leaving `data-sveltekit-reload` on a
	// row that no longer needs it. The attribute would keep the page working and cost a full document
	// load on every click, and nothing else in the estate would notice.
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
		page.waitForURL('**/settings/access'),
		page.getByRole('link', { name: /^Users & roles\b/ }).click(),
	]);

	expect(await page.evaluate(() => '__sameDocument' in window)).toBe(true);
	// …and the router raised nothing on the way: a soft nav into a route the manifest DOES contain has
	// no 404 branch to fall through, so this zone never blames itself for its own page.
	const zoneErrors = await page.evaluate(() => sessionStorage.getItem('__zoneErrors'));
	expect(zoneErrors, 'an in-app navigation must not raise a client-side error').toBeNull();
});

test('the unwired rows read as decisions, not as broken controls', async ({ page }) => {
	await seed(page, { 'GET /v1/me': ME_ADMIN, 'GET /v1/projects': [] });
	await page.goto('/settings');

	// Notifications LEFT this list when its page landed: it is a real route now (`/settings/notifications`,
	// over the service's own `GET|PUT /prefs` + `/watches`), so asserting it as unwired would pin the
	// page to a state it has grown out of.
	for (const title of ['New-project defaults', 'Credentials']) {
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
