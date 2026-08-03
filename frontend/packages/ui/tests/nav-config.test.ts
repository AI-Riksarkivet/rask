import { describe, expect, it } from 'vitest';
import { exact, mainMenuNav, norm, seg, topNav, under, zoneOf } from '../src/lib/shell/nav-config';

// The top-navbar IA + the shared matchers every zone builds its ZoneNav sidebar config with.
// ONE ENTRY PER ZONE, and EVERY zone of the seven-zone estate is in the bar (R15) — home, lakehouse,
// media (Search), annotator (Annotate), compute, train, studio. One column per area: Lakehouse
// covers the whole merged /lakehouse zone — the catalog, the model registry, lineage, and
// (estate-admin only) governance and operations. Lineage used to be its own trigger, which made the
// bar mix a zone with an area inside that zone and forced Lakehouse to subtract the lineage subtree
// from its own match; it is a column now. A new route becomes a row in a column — only a new ZONE
// earns a new top-level entry.
//
// ONE exception, by ruling (2026-08-03, "projects and settings in topnavbar in main menu"):
// `Projects` is a route in the HOME zone, not a zone. It earns an entry because a project is the TOP
// of the hierarchy (project › warehouse › namespace › table) — not a peer of the zones in this bar
// but the thing they are scoped by — so it leads it.
describe('topNav', () => {
	it('carries every zone of the estate, in order, for a non-admin (fail-closed)', () => {
		// No 'Home': the origin root is reached by the project switcher and the sidebar header, not
		// by a third control in a bar whose job is moving BETWEEN zones.
		// Lakehouse and Compute LEAD the bar and are tier 'primary': the lakehouse you govern and the
		// compute that fills it are where the work happens. The rest are real zones but task
		// destinations, so they follow and read one step quieter — six equally-weighted entries told a
		// newcomer nothing about where to start.
		expect(topNav(false).map((e) => e.title)).toEqual([
			'Projects',
			'Lakehouse',
			'Compute',
			'Search',
			'Annotate',
			'Train',
			'Studio',
		]);
		expect(
			topNav(false)
				.filter((e) => e.tier === 'primary')
				.map((e) => e.title),
		).toEqual(['Projects', 'Lakehouse', 'Compute']);
		expect(topNav(false).map((e) => e.href)).toEqual([
			// `/projects` is a ROUTE in the home zone, not a zone base, so it carries NO trailing slash
			// — SvelteKit's default trailingSlash is 'never' and '/projects/' would cost the 308 the
			// zone-root slashes below exist to avoid. Opposite rule, same reason: match what is served.
			'/projects',
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

	it('gives an estate admin exactly ONE extra entry — Settings — and it comes last', () => {
		// This used to assert the two bars were IDENTICAL ("admin earns COLUMNS, not an entry"), which
		// was the right rule while governance was a column of the Lakehouse panel. The 2026-08-03
		// ruling moved it out ("governance … under settings, in the topnavbar in main menu"), so the
		// admin bar legitimately carries one more entry. The invariant that actually mattered is kept
		// and made explicit: the extra entry is Settings and NOTHING else differs.
		const base = topNav(false).map((e) => e.title);
		expect(topNav(true).map((e) => e.title)).toEqual([...base, 'Settings']);
	});

	it('gates OPERATIONS behind estate-admin inside the Lakehouse panel — governance is not there', () => {
		const labels = (admin: boolean) =>
			topNav(admin)
				.find((e) => e.title === 'Lakehouse')!
				.groups!.map((g) => g.label);
		// The governance guarantee, both polarities: a non-admin's panel cannot even name them.
		expect(labels(false)).toEqual(['Catalog', 'Models', 'Lineage']);
		// Operations — streams, events, dead letters — is an operation ON the lakehouse, so it stays a
		// column here. Governance is not a lakehouse feature and no longer appears in this panel at
		// all; asserted as an ABSENCE so it cannot quietly return and exist in two places.
		expect(labels(true)).toEqual(['Catalog', 'Models', 'Lineage', 'Operations']);
		expect(labels(true)).not.toContain('Governance');
	});

	it('never exposes Access as a navbar entry — it is one row of the Settings panel', () => {
		expect(topNav(false).map((e) => e.title)).not.toContain('Access');
		expect(topNav(true).map((e) => e.title)).not.toContain('Access');
		const settings = topNav(true).find((e) => e.title === 'Settings')!;
		expect(settings.items!.find((i) => i.title === 'Access')?.href).toBe(
			'/lakehouse/governance/access',
		);
		// …and a non-admin gets no row carrying it at all, in any panel.
		const nonAdminHrefs = topNav(false).flatMap((e) => [
			...(e.items ?? []),
			...(e.groups ?? []).flatMap((g) => g.items),
		]);
		expect(nonAdminHrefs.map((i) => i.href)).not.toContain('/lakehouse/governance/access');
	});

	it('the MAIN MENU carries two entries — Projects and Settings — and nothing else', () => {
		// By ruling (2026-08-03): "we should only see 2 items in topnavbar — projects and settings,
		// nothing else". Standing at the estate root you are choosing what to work on, not moving
		// between zones; the full zone bar there answers a question nobody has asked yet. The shell
		// swaps on `zoneOf(pathname) === ''`, so this is the whole contract of that swap.
		expect(mainMenuNav(true).map((e) => e.title)).toEqual(['Projects', 'Settings']);
		// A non-admin gets ONE entry, not a disabled second one — Settings is absent, fail-closed.
		expect(mainMenuNav(false).map((e) => e.title)).toEqual(['Projects']);
		// No zone leaks in, in either identity — the assertion that catches a new zone being added to
		// `topNav` and silently appearing at the estate root as well.
		for (const admin of [false, true]) {
			const titles = mainMenuNav(admin).map((e) => e.title);
			for (const zone of [
				'Lakehouse',
				'Compute',
				'Workbench',
				'Search',
				'Annotate',
				'Train',
				'Studio',
			]) {
				expect(titles).not.toContain(zone);
			}
		}
		// …and each entry is the zone bar's entry, not a second declaration of it: same href, same
		// active-matching. `topNav` rebuilds its array per call, so identity is not the test — drift
		// between two hand-written copies is what this catches.
		for (const title of ['Projects', 'Settings']) {
			const fromMain = mainMenuNav(true).find((e) => e.title === title)!;
			const fromZoneBar = topNav(true).find((e) => e.title === title)!;
			expect(fromMain.href).toBe(fromZoneBar.href);
			for (const p of ['/projects', '/projects/acme', '/lakehouse/governance/access', '/media/']) {
				expect(fromMain.match(p)).toBe(fromZoneBar.match(p));
			}
		}
	});

	it('closes the ruling: Settings carries governance, is estate-admin ONLY, and comes last', () => {
		// The other half of "projects and settings in topnavbar in main menu". Fail-closed like the
		// column it replaced: ABSENT for a non-admin rather than present-and-disabled, so the bar never
		// names a surface the viewer is barred from.
		expect(topNav(false).some((e) => e.title === 'Settings')).toBe(false);
		const admin = topNav(true);
		expect(admin.at(-1)!.title).toBe('Settings');
		const settings = admin.find((e) => e.title === 'Settings')!;
		expect(settings.items!.map((i) => i.title)).toEqual(['Access', 'Tenants', 'Audit']);
		// It lights across the governance surfaces wherever they are served from today…
		expect(settings.match('/lakehouse/governance/access')).toBe(true);
		expect(settings.match('/lakehouse/governance/audit')).toBe(true);
		// …and never steals the highlight from the zone that still hosts those routes' neighbours.
		expect(settings.match('/lakehouse/catalog')).toBe(false);
		expect(settings.match('/projects')).toBe(false);
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

	it('leads with Projects — the top of the hierarchy, and NOT a second Home', () => {
		// The 2026-08-03 ruling: there is one project concept, it is the top of the hierarchy, and its
		// list/overview/create surfaces are main-menu pages in the home zone. It is a plain link (one
		// surface plus its per-project detail — a one-row dropdown would be noise) and it matches the
		// whole subtree so `/projects/<p>` keeps it lit.
		for (const admin of [false, true]) {
			const projects = topNav(admin).find((e) => e.title === 'Projects')!;
			expect(projects.href).toBe('/projects');
			expect(projects.items).toBeUndefined();
			expect(projects.groups).toBeUndefined();
			expect(projects.match('/projects')).toBe(true);
			expect(projects.match('/projects/acme')).toBe(true);
			// The origin root is the LANDING, a different page — this entry must never claim it, or the
			// bar has grown the Home entry the assertion below forbids, spelled differently.
			expect(projects.match('/')).toBe(false);
			expect(projects.match('/lakehouse/catalog')).toBe(false);
		}
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
		//
		// NO 'Projects' ROW, by ruling (2026-08-03, f5dd1f0 — the dropdown loses its filler self-row
		// and the tenants list): a project is the TOP of the hierarchy — project › warehouse ›
		// namespace › table — so listing "projects" as a row INSIDE one project's catalog described the
		// lakekeeper API's tenant list, not this product's model. The estate has one project concept,
		// reached from the switcher, never from a row under one zone's catalog column.
		expect(groups.Catalog).toEqual(['Tables', 'Namespaces', 'Warehouses', 'Storage']);
		expect(groups.Models).toEqual(['Registry', 'Experiments', 'Pipeline']);
		// No Governance column here any more — it is the Settings entry's panel (2026-08-03 ruling),
		// asserted in full by 'closes the ruling: Settings carries governance…' above. Pinned as an
		// absence so the rows cannot come to exist in both places.
		expect(groups.Governance).toBeUndefined();
		expect(groups.Operations).toEqual(['Events', 'Streams', 'DLQ']);
		expect(groups.Lineage).toEqual(['Datasets', 'Jobs', 'Runs', 'Columns', 'Graph']);
	});

	it('Media carries its rows too — the panel that was never pinned', () => {
		// Compute's rows and all five Lakehouse columns are asserted above; MEDIA's never were, so its
		// panel could gain or lose a row with this suite still green. That omission had teeth: media is
		// one of the three dock zones, `dock-reachability.test.ts` requires /media/workbench to appear
		// here, and nothing in this file would have noticed it going missing.
		const media = topNav(false).find((e) => e.title === 'Search')!;
		expect(media.items!.map((i) => i.title)).toEqual([
			'Search',
			'Atlas',
			'Tree',
			'Graph',
			// The zone's own dock — its panels ARE this zone's components over one shared search
			// store, so it is a row of this panel like every other area of the zone.
			'Workbench',
			'Workflow',
		]);
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
