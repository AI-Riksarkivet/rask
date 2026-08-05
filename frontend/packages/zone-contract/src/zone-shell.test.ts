import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

const ZONES = zoneDirs();

/**
 * Every zone renders the SAME shell.
 *
 * Two zones used to opt out silently. `annotator` passed no `zoneNav` at all and `studio` never had
 * a nav config written, so both rendered no sidebar — landing in either from anywhere else stripped
 * away the navigation the rest of the estate provides, with nothing saying so. Nothing failed,
 * because nothing checked: the shell treats a missing `zoneNav` as "this zone has none", which is
 * indistinguishable from "nobody wired it up yet".
 *
 * These assertions are deliberately structural (source text, not a rendered DOM): a zone that
 * forgets the wiring should fail in unit tests, long before someone notices a missing rail in a
 * browser.
 *
 * A zone MAY have no rail — but it must SAY SO. `NAVLESS` below is that declaration. The property
 * this file protects has never been "every zone has a sidebar"; it is "no zone loses one by
 * accident", and the difference between the two is exactly `zoneNav={null}` written on purpose
 * versus a prop nobody passed.
 */

/**
 * Zones that deliberately render NO zone rail, with the reason. Adding one here is a design
 * decision that should be argued in review, which is the point of making it a list.
 *
 * `annotator` was here, and came OUT — the entry was reasoning about the wrong cost. It argued that
 * the zone's two rows read as a second sidebar beside the annotate view's annotation panel, which
 * is a fair point about the CANVAS. But `zoneNav={null}` does not remove two rows: `AppShell` gates
 * the WHOLE `<AppSidebar>` on `zoneNavLeaves(zoneNav).length > 1`, and the sidebar HEADER is where
 * the project switcher lives. So the opt-out removed the project dropdown, the estate zone links
 * and the collapse trigger — landing in Annotate stripped away navigation every other zone gives
 * you, which is precisely the failure this file exists to prevent and the one `app-shell.svelte`
 * documents having already fixed for canvas mode. The canvas concern is handled there instead: a
 * canvas zone starts the rail icon-collapsed.
 *
 * The list is empty on purpose. An entry costs a whole sidebar, not a row — weigh it that way.
 */
const NAVLESS = new Set<string>();

const zoneDir = (zone: string) => resolve(FRONTEND_ROOT, 'microfrontends', zone);
const navPath = (zone: string) => resolve(zoneDir(zone), 'src', 'lib', 'nav.ts');
const layoutPath = (zone: string) => resolve(zoneDir(zone), 'src', 'routes', '+layout.svelte');

describe('every zone ships a sidebar config', () => {
	for (const zone of ZONES) {
		it(`${zone} declares a ZoneNav`, () => {
			if (NAVLESS.has(zone)) {
				expect(
					existsSync(navPath(zone)),
					`${zone} renders no rail, so a nav.ts is dead code — delete it or drop the zone from NAVLESS`,
				).toBe(false);
				return;
			}
			expect(existsSync(navPath(zone)), `${zone}/src/lib/nav.ts is missing`).toBe(true);
		});

		it(`${zone} passes zoneNav to AppShell`, () => {
			const layout = readFileSync(layoutPath(zone), 'utf8');
			// Both branches require the prop to be PRESENT — a navless zone still has to write
			// `zoneNav={null}`, so the opt-out is greppable and survives review.
			expect(layout, `${zone}'s root +layout.svelte never passes zoneNav to <AppShell>`).toMatch(
				/zoneNav[=\s]/,
			);
			if (NAVLESS.has(zone)) {
				expect(
					layout,
					`${zone} is listed in NAVLESS, so it must opt out explicitly with zoneNav={null}`,
				).toMatch(/zoneNav=\{null\}/);
			}
		});

		it(`${zone}'s nav is GROUPED, not a flat leaves[] list`, () => {
			if (NAVLESS.has(zone)) return;
			const nav = readFileSync(navPath(zone), 'utf8');
			expect(nav, `${zone} still declares a flat leaves[] — ZoneNav is groups[] now`).not.toMatch(
				/^\s*leaves:/m,
			);
			// Matches both the inline form (`groups: [`) and a hoisted const typed as
			// ZoneNav['groups'], which lakehouse uses so its privilege filter can slice the array.
			expect(nav, `${zone} declares no groups[]`).toMatch(/groups(:|'\])\s*(=\s*)?\[/);
		});
	}
});

describe('a zone with nothing to navigate renders no rail', () => {
	// The shell suppresses the sidebar below two leaves, so a one-row rail linking to the page you
	// are already on cannot appear. `home` is the standing example — its only leaf is the project
	// gallery, which IS its landing.
	// Count `href:`, NOT `title:` — every leaf has exactly one href, whereas `title:` also matches
	// the ZoneNav's own display name and would over-count every zone by one.
	const countLeaves = (src: string) => (src.match(/\bhref:\s*'/g) ?? []).length;

	it('home declares exactly one leaf, so its rail is suppressed', () => {
		const leaves = countLeaves(readFileSync(navPath('home'), 'utf8'));
		expect(leaves, 'home gained leaves — it should stay the single-leaf catch-all').toBe(1);
	});

	// `annotator` (NAVLESS) is exempt on the rule's own principle: it renders its own annotation
	// panel and ships no nav.ts at all, because a two-row rail beside that panel read as two sidebars
	// competing for one job. Its opt-out is enforced above by the zoneNav={null} assertion.
	for (const zone of ZONES.filter((z) => z !== 'home' && !NAVLESS.has(z))) {
		it(`${zone} declares more than one leaf, so its rail renders`, () => {
			const leaves = countLeaves(readFileSync(navPath(zone), 'utf8'));
			expect(
				leaves,
				`${zone} has ${leaves} leaf(s); the shell hides a rail below 2, so this zone would ` +
					`silently render no sidebar`,
			).toBeGreaterThan(1);
		});
	}
});

describe('canvas mode does not opt a zone out of navigation', () => {
	it('the shell renders the sidebar independently of `canvas`', () => {
		const shell = readFileSync(
			resolve(FRONTEND_ROOT, 'packages', 'ui', 'src', 'lib', 'shell', 'app-shell.svelte'),
			'utf8',
		);
		// The regression: `{#if !canvas}` wrapping the <AppSidebar>, which is what left the annotator
		// as the one zone in the estate with no rail.
		expect(shell, 'canvas mode suppresses the sidebar again').not.toMatch(
			/\{#if\s+!canvas\}\s*\{#if\s+hasNav\}/,
		);
	});
});

describe('home stays reachable after leaving the zone navbar', () => {
	// `home` used to carry a navbar entry like every other zone. It no longer does: it is the catch-all
	// zone, and a row for it would, on home itself, link to the page you are standing on. Reachability
	// therefore rests entirely on the shell's project switcher — so it is asserted, not assumed. Without
	// this, dropping the navbar row and later refactoring the sidebar header would strand the zone with
	// no route to it from anywhere, and the e2e that used to catch that (home/e2e/auth.spec.ts's
	// one-entry-per-zone check) no longer covers home by construction.
	// The link lives in the SWITCHER, which IS what the sidebar header renders. It moved there when the
	// header stopped printing the zone's name: the switcher's own "Main menu" item is the way out, so a
	// second Home row above it was two doors to one room. The guard follows the link, not the file.
	const switcher = readFileSync(
		resolve(FRONTEND_ROOT, 'packages', 'ui', 'src', 'lib', 'shell', 'project-switcher.svelte'),
		'utf8',
	);

	it('the sidebar header offers a way home', () => {
		expect(
			switcher,
			'the project switcher no longer links home — the home zone is now unreachable',
		).toMatch(/href=\{homeUrl\}/);
	});

	it('and hard-navigates, because every other zone is a different app', () => {
		// A soft nav from e.g. /lakehouse to / asks the lakehouse app for a route it does not own → 404.
		const brand = /<a[^>]*href=\{homeUrl\}[^>]*>/.exec(switcher)?.[0] ?? '';
		expect(brand, `brand anchor lacks data-sveltekit-reload: ${brand}`).toMatch(
			/data-sveltekit-reload/,
		);
	});
});
