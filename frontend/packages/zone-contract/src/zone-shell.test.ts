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
 */

const zoneDir = (zone: string) => resolve(FRONTEND_ROOT, 'microfrontends', zone);
const navPath = (zone: string) => resolve(zoneDir(zone), 'src', 'lib', 'nav.ts');
const layoutPath = (zone: string) => resolve(zoneDir(zone), 'src', 'routes', '+layout.svelte');

describe('every zone ships a sidebar config', () => {
	for (const zone of ZONES) {
		it(`${zone} declares a ZoneNav`, () => {
			expect(existsSync(navPath(zone)), `${zone}/src/lib/nav.ts is missing`).toBe(true);
		});

		it(`${zone} passes zoneNav to AppShell`, () => {
			const layout = readFileSync(layoutPath(zone), 'utf8');
			expect(layout, `${zone}'s root +layout.svelte never passes zoneNav to <AppShell>`).toMatch(
				/zoneNav[=\s]/,
			);
		});

		it(`${zone}'s nav is GROUPED, not a flat leaves[] list`, () => {
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

	for (const zone of ZONES.filter((z) => z !== 'home')) {
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
