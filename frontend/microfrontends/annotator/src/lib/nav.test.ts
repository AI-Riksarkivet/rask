/**
 * The annotate rail names JOBS, not screens.
 *
 * Reported: "why is there labeling tasks and group data browse-corpus? and not labeling tasks and
 * bulk labeling, and LLM-as-a-judge, AI-assistance, Annotation settings in the sidebar".
 *
 * The old rail had exactly two rows and both were named after what happened to be built — a group
 * called "Data" holding "Browse corpus". Everything else this zone does was absent from the rail, so
 * a reader could not distinguish "not built yet" from "this product does not do that". Three of the
 * five rows now lead to routes that say, in as many words, that they are not built.
 *
 * These assertions are about the CONTRACT between the rail and the router: every row is a real
 * route, and the labels are the ones asked for. A row pointing at a path SvelteKit does not serve is
 * a 404 the rail itself creates, which is the failure mode `/annotator/projects` had.
 */

import type { ZoneNavLeaf } from '@rask/ui/shell';
import { describe, expect, it } from 'vitest';

import { ANNOTATOR_ZONE_NAV } from './nav';

/** The zone root, narrowed.
 *
 *  `ZoneNav.root` is optional, and this rail must have one — it is the row `AppShell` renders above
 *  every group, and losing it is how this zone once lost its whole sidebar (see nav.ts). Throwing
 *  rather than asserting non-null keeps that a checked fact instead of a silenced type error. */
function root(): ZoneNavLeaf {
	const r = ANNOTATOR_ZONE_NAV.root;
	if (!r) throw new Error('the annotate rail has no zone root');
	return r;
}

/** Every leaf in rail order: the zone root first, then each group's items. */
function leaves(): ZoneNavLeaf[] {
	return [root(), ...ANNOTATOR_ZONE_NAV.groups.flatMap((g) => g.items)];
}

/** Just the parts the rail SHOWS and the router must serve. */
function rows(): { title: string; href: string }[] {
	return leaves().map((l) => ({ title: l.title, href: l.href }));
}

describe('the five rows', () => {
	it('names every job the zone does, in order', () => {
		expect(rows().map((r) => r.title)).toEqual([
			'Labeling tasks',
			'Bulk labeling',
			'LLM-as-a-judge',
			'AI assistance',
			'Annotation settings',
		]);
	});

	it('calls the corpus browser by its JOB, not its mechanism', () => {
		// The path stays `/browse` — it IS the corpus browser, and a bulk send starts there (#42).
		// Only the row is renamed, because "browse corpus" says how and "Bulk labeling" says why.
		const bulk = rows().find((r) => r.title === 'Bulk labeling');

		expect(bulk?.href).toBe('/annotator/browse');
		expect(rows().map((r) => r.title)).not.toContain('Browse corpus');
	});

	it('has no group called "Data" — every row is data', () => {
		expect(ANNOTATOR_ZONE_NAV.groups.map((g) => g.label)).not.toContain('Data');
	});

	it('never repeats a group label', () => {
		// The defect #52 fixed in the explorer rail: two groups sharing a heading draw the same title
		// twice with one row under each, which reads as a rendering bug.
		const labels = ANNOTATOR_ZONE_NAV.groups.map((g) => g.label);

		expect(labels).toEqual([...new Set(labels)]);
	});
});

describe('every row addresses a route this zone serves', () => {
	it('points every href at the zone base', () => {
		// Absolute domain paths, because the zone is served under `/annotator` both standalone and
		// behind the ingress. A bare `/judge` would leave the zone and 404 at the proxy.
		for (const row of rows()) expect(row.href.startsWith('/annotator')).toBe(true);
	});

	it('has a +page.svelte behind each one', async () => {
		// The gate that would have caught `/annotator/projects`: a rail row whose route does not
		// exist is a 404 the rail creates. `import.meta.glob` sees the real route tree, so adding a
		// row without a page fails here rather than in someone's browser.
		const pages = import.meta.glob('../routes/**/+page.svelte');
		const served = new Set(
			Object.keys(pages).map(
				(p) => `/annotator${p.replace('../routes', '').replace('/+page.svelte', '')}`,
			),
		);

		for (const row of rows()) expect([...served]).toContain(row.href);
	});
});

describe('the active-route predicates', () => {
	it('lights the ROOT only on the zone root', () => {
		// `exact`, not `seg` — the bug #29 fixed in the compute rail. `seg` would light "Labeling
		// tasks" on every other page in the zone.
		const { match } = root();

		expect(match('/annotator')).toBe(true);
		expect(match('/annotator/browse')).toBe(false);
		expect(match('/annotator/projects/bind86')).toBe(false);
	});

	it('lights exactly ONE row per path', () => {
		// Zero lit rows leaves the rail looking like you are nowhere; two makes it lie about where you
		// are. Both are the same class of bug as #29 and neither had a gate.
		for (const { href } of rows()) {
			expect(leaves().filter((l) => l.match(href))).toHaveLength(1);
		}
	});
});
