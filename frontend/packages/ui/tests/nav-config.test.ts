import { describe, expect, it } from 'vitest';
import { exact, norm, seg, topNav, under, zoneOf } from '../src/lib/shell/nav-config';

// The top-navbar IA + the shared matchers every zone builds its ZoneNav sidebar config with.
// ONE ENTRY PER ZONE, and EVERY zone of the seven-zone estate is in the bar (R15) — home, lakehouse,
// media (Search), annotator (Annotate), compute, train, studio. One column per area: Lakehouse
// covers the whole merged /lakehouse zone — the catalog, the model registry, lineage, and
// (estate-admin only) governance and operations. Lineage used to be its own trigger, which made the
// bar mix a zone with an area inside that zone and forced Lakehouse to subtract the lineage subtree
// from its own match; it is a column now. A new route becomes a row in a column — only a new ZONE
// earns a new top-level entry.
describe('topNav', () => {
	it('carries every zone of the estate, in order, for a non-admin (fail-closed)', () => {
		// No 'Home': the origin root is reached by the project switcher and the sidebar header, not
		// by a third control in a bar whose job is moving BETWEEN zones.
		// Lakehouse and Compute LEAD the bar and are tier 'primary': the lakehouse you govern and the
		// compute that fills it are where the work happens. The rest are real zones but task
		// destinations, so they follow and read one step quieter — six equally-weighted entries told a
		// newcomer nothing about where to start.
		expect(topNav(false).map((e) => e.title)).toEqual([
			'Lakehouse',
			'Compute',
			'Search',
			'Annotate',
			'Train',
			'Studio',
		]);
		expect(topNav(false).filter((e) => e.tier === 'primary').map((e) => e.title)).toEqual([
			'Lakehouse',
			'Compute',
		]);
		expect(topNav(false).map((e) => e.href)).toEqual([
			'/lakehouse/catalog',
			'/compute/',
			// Trailing slashes are LOAD-BEARING, not cosmetic: each zone's `paths.base` serves the
			// trailing form, so a bare '/compute' href cost a 308 redirect round-trip on EVERY
			// cross-zone hop (measured on all five zones, 2026-07-28) — visible as flicker over a
			// tunnel. The href must be what the zone actually serves.
			'/media/',
			'/annotator/',
			'/train/',
			'/studio/',
		]);
	});

	it('keeps the same entries for an estate admin — admin earns COLUMNS, not an entry', () => {
		expect(topNav(true).map((e) => e.title)).toEqual(topNav(false).map((e) => e.title));
	});

	it('gates governance + operations behind estate-admin, inside the Lakehouse panel', () => {
		const labels = (admin: boolean) =>
			topNav(admin)
				.find((e) => e.title === 'Lakehouse')!
				.groups!.map((g) => g.label);
		// The governance guarantee, both polarities: a non-admin's panel cannot even name them.
		expect(labels(false)).toEqual(['Catalog', 'Models', 'Lineage']);
		expect(labels(true)).toEqual(['Catalog', 'Models', 'Lineage', 'Governance', 'Operations']);
	});

	it('never exposes Access as a navbar entry — it is one row of the Governance column', () => {
		expect(topNav(false).map((e) => e.title)).not.toContain('Access');
		expect(topNav(true).map((e) => e.title)).not.toContain('Access');
		const governance = topNav(true)
			.find((e) => e.title === 'Lakehouse')!
			.groups!.find((g) => g.label === 'Governance')!;
		expect(governance.items.find((i) => i.title === 'Access')?.href).toBe(
			'/lakehouse/governance/access',
		);
		// …and a non-admin gets no row carrying it at all, in any panel.
		const nonAdminHrefs = topNav(false).flatMap((e) => [
			...(e.items ?? []),
			...(e.groups ?? []).flatMap((g) => g.items),
		]);
		expect(nonAdminHrefs.map((i) => i.href)).not.toContain('/lakehouse/governance/access');
	});

	it('active-match: Lakehouse lights across every area of its zone, lineage included', () => {
		// One trigger owns the whole zone, so there is no subtree to subtract and no pair of entries
		// that can light up together.
		const lakehouse = topNav(true).find((e) => e.title === 'Lakehouse')!;
		for (const p of [
			'/lakehouse/catalog',
			'/lakehouse/catalog/tables/db$t',
			'/lakehouse/models',
			'/lakehouse/models/pipeline',
			'/lakehouse/governance/audit',
		]) {
			expect(lakehouse.match(p)).toBe(true);
		}
		expect(lakehouse.match('/lakehouse/lineage')).toBe(true);
		expect(lakehouse.match('/lakehouse/lineage/runs')).toBe(true);
		expect(lakehouse.match('/')).toBe(false);
		expect(lakehouse.match('/media')).toBe(false);
		expect(lakehouse.match('/annotator')).toBe(false);
		expect(lakehouse.match('/compute')).toBe(false);
	});

	it('lineage is a COLUMN of the lakehouse panel, never a trigger of its own', () => {
		expect(topNav(true).map((e) => e.title)).not.toContain('Lineage');
		const lakehouse = topNav(true).find((e) => e.title === 'Lakehouse')!;
		expect(lakehouse.groups!.find((g) => g.label === 'Lineage')).toBeDefined();
		// …and the trigger claims the lineage routes, so it lights up while you are in there.
		expect(lakehouse.match('/lakehouse/lineage')).toBe(true);
		expect(lakehouse.match('/lakehouse/lineage/runs')).toBe(true);
	});

	it('Search and Annotate are separate zones, so separate triggers', () => {
		// One trigger per zone: the annotator is its own microfrontend, so it gets its own entry
		// rather than hiding as a row in Search's panel. Neither trigger claims the other's zone.
		const search = topNav(false).find((e) => e.title === 'Search')!;
		const annotate = topNav(false).find((e) => e.title === 'Annotate')!;
		expect(search.match('/media')).toBe(true);
		expect(search.match('/annotator')).toBe(false);
		expect(annotate.match('/annotator')).toBe(true);
		expect(annotate.match('/media')).toBe(false);
		// Annotate is a single surface — a plain link, not a one-row dropdown.
		expect(annotate.items).toBeUndefined();
		expect(annotate.groups).toBeUndefined();
		// …and Annotate is no longer buried inside Search's panel.
		expect(search.items?.some((i) => i.href === '/annotator')).toBe(false);
	});

	it('carries NO Home entry — the origin root is not a peer zone', () => {
		// It used to ride here as an entry like any other (R15: every zone in the bar). That made the
		// estate's landing surface look like a sibling of Lakehouse and Compute rather than the thing
		// containing them, and duplicated a destination the project switcher and the sidebar header
		// already own. Pinned as an absence so it cannot drift back in unnoticed.
		for (const admin of [false, true]) {
			expect(topNav(admin).some((e) => e.title === 'Home')).toBe(false);
			expect(topNav(admin).some((e) => e.href === '/')).toBe(false);
		}
	});

	it('Compute owns the Ray plane — the folded overview at its root plus the Ray surfaces (R16)', () => {
		const compute = topNav(false).find((e) => e.title === 'Compute')!;
		expect(compute.match('/compute')).toBe(true);
		expect(compute.match('/compute/jobs/raysubmit_123')).toBe(true);
		expect(compute.match('/media')).toBe(false);
		expect(compute.match('/')).toBe(false);
		// The overview IS the zone root (like Media's Search), so the first row carries entry.href
		// and the panel never prepends a second zone-root row.
		expect(compute.items!.map((i) => i.title)).toEqual([
			'Overview',
			'Jobs',
			'Cluster',
			'Actors',
			'Serve',
			'Logs',
			'API docs',
		]);
		// Trailing form: the zone's paths.base serves '/compute/', so the bare href cost a 308 per hop.
		expect(compute.items![0]!.href).toBe('/compute/');
	});

	it('Train and Studio are single-surface zones — plain links with disjoint matches (R17)', () => {
		const train = topNav(false).find((e) => e.title === 'Train')!;
		const studio = topNav(false).find((e) => e.title === 'Studio')!;
		// Train's areas (submit/watch/monitor/analyse/models) are still scaffolding; studio is the
		// sandbox. Neither has panel rows yet — a one-row dropdown would be noise.
		for (const entry of [train, studio]) {
			expect(entry.items).toBeUndefined();
			expect(entry.groups).toBeUndefined();
		}
		expect(train.match('/train')).toBe(true);
		expect(train.match('/train/submit')).toBe(true);
		expect(train.match('/studio')).toBe(false);
		expect(studio.match('/studio')).toBe(true);
		expect(studio.match('/studio/animation')).toBe(true);
		expect(studio.match('/train')).toBe(false);
	});

	it('carries the expected rows per column', () => {
		const groups = Object.fromEntries(
			topNav(true)
				.find((e) => e.title === 'Lakehouse')!
				.groups!.map((g) => [g.label, g.items.map((i) => i.title)]),
		);
		// R28: Storage joins the panel — the object browser was previously reachable ONLY by typing
		// the URL, because the lakehouse sidebar is area-scoped and no other area linked to it.
		expect(groups.Catalog).toEqual(['Projects', 'Tables', 'Namespaces', 'Warehouses', 'Storage']);
		expect(groups.Models).toEqual(['Registry', 'Experiments', 'Pipeline']);
		expect(groups.Governance).toEqual(['Access', 'Tenants', 'Audit']);
		expect(groups.Operations).toEqual(['Events', 'Streams', 'DLQ']);
		expect(groups.Lineage).toEqual(['Datasets', 'Jobs', 'Runs', 'Columns', 'Graph']);
	});

	it('every entry is reachable: a panel with rows, or a plain link', () => {
		for (const entry of topNav(true)) {
			expect(entry.href.startsWith('/'), `${entry.title} href`).toBe(true);
			const rows = [...(entry.items ?? []), ...(entry.groups ?? []).flatMap((g) => g.items)];
			// A zone with ONE surface is a plain link (Annotate) — no rows to check, and a one-row
			// dropdown would be noise. A zone with a panel must not ship an EMPTY panel, which would
			// render a trigger that opens onto nothing.
			if (entry.items === undefined && entry.groups === undefined) continue;
			expect(rows.length, `${entry.title} panel is empty`).toBeGreaterThan(0);
			for (const item of rows) {
				expect(item.href.startsWith('/')).toBe(true);
				expect(item.description.length).toBeGreaterThan(0);
			}
		}
	});
});

describe('ZoneNav matchers', () => {
	it('seg: matches the exact route and anything nested under it', () => {
		const m = seg('/lakehouse/catalog/tables');
		expect(m('/lakehouse/catalog/tables')).toBe(true);
		expect(m('/lakehouse/catalog/tables/x')).toBe(true);
		expect(m('/lakehouse/catalog/namespaces')).toBe(false);
	});

	it('exact: matches only its own path — the root-leaf (href == zone href) case', () => {
		// Registry (=/lakehouse/models) sits at its AREA root; `seg` would keep it lit on every sub-route.
		const m = exact('/lakehouse/models');
		expect(m('/lakehouse/models')).toBe(true);
		expect(m('/lakehouse/models/pipeline')).toBe(false);
	});

	it('norm + matchers tolerate the base-path trailing slash on a zone root', () => {
		// A zone served under a base path reports its root as `/lakehouse/` (trailing slash).
		expect(norm('/lakehouse/')).toBe('/lakehouse');
		expect(norm('/')).toBe('/');
		expect(exact('/lakehouse')('/lakehouse/')).toBe(true);
		expect(under('/lakehouse')('/lakehouse/')).toBe(true);
	});

	it('zoneOf: first path segment, with the home zone at the empty key', () => {
		// The whole estate is ONE zone now, so every lakehouse area shares a zone key — which is what
		// makes a hop between them a soft nav rather than a full document load.
		expect(zoneOf('/lakehouse/catalog/tables')).toBe('lakehouse');
		expect(zoneOf('/lakehouse/admin')).toBe('lakehouse');
		expect(zoneOf('/media/atlas')).toBe('media');
		expect(zoneOf('/')).toBe('');
		expect(zoneOf('')).toBe('');
	});
});
