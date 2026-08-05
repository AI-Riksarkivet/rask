import { describe, expect, it } from 'vitest';
import {
	exact,
	isMainMenu,
	mainMenuNav,
	norm,
	seg,
	topNav,
	under,
	zoneOf,
} from '../src/lib/shell/nav-config';

// The top-navbar IA + the shared matchers every zone builds its ZoneNav sidebar config with.
// ONE ENTRY PER ZONE, and EVERY zone of the seven-zone estate is in the bar (R15) — home, lakehouse,
// explorer (Explorer), annotator (Annotate), compute, train, studio. One column per area: Lakehouse
// covers the whole merged /lakehouse zone — the catalog, the model registry, lineage, and
// (estate-admin only) operations. Lineage used to be its own trigger, which made the
// bar mix a zone with an area inside that zone and forced Lakehouse to subtract the lineage subtree
// from its own match; it is a column now. A new route becomes a row in a column — only a new ZONE
// earns a new top-level entry.
//
// ONE exception, by ruling (2026-08-03, "projects and settings in topnavbar in main menu"):
// `Projects` is a route in the HOME zone, not a zone. It earns an entry because a project is the TOP
// of the hierarchy (project › warehouse › namespace › table) — not a peer of the zones in this bar
// but the thing they are scoped by — so it leads it.
describe('topNav', () => {
	it('is the IN-PROJECT bar: one entry per zone, and nothing that is not a zone', () => {
		// `topNav` is now ONE of two bars (the two-level ruling): this is what you get INSIDE a project.
		// No 'Home', no 'Projects', no 'Settings' — those are the estate level and live in
		// `mainMenuNav`. Every zone directory except home appears exactly once (R15), so a zone
		// scaffolded without an entry fails here.
		expect(topNav(false).map((e) => e.title)).toEqual([
			'Lakehouse',
			'Compute',
			'Explorer',
			'Annotate',
			'Models',
			'Studio',
		]);
		// Lakehouse and Compute LEAD and are tier 'primary' — the lakehouse you govern and the compute
		// that fills it are where work happens; the shell renders a visible GAP after them, and
		// everything past it is a task destination. `tier` was already in the data and nothing rendered
		// it, so eight equally-weighted chips told a newcomer nothing about where to start.
		expect(
			topNav(false)
				.filter((e) => e.tier === 'primary')
				.map((e) => e.title),
		).toEqual(['Lakehouse', 'Compute']);
		expect(topNav(false).map((e) => e.href)).toEqual([
			'/lakehouse/catalog',
			'/compute/',
			// Trailing slashes are LOAD-BEARING, not cosmetic: each zone's `paths.base` serves the
			// trailing form, so a bare '/compute' href cost a 308 redirect round-trip on EVERY
			// cross-zone hop (measured on all five zones, 2026-07-28) — visible as flicker over a
			// tunnel. The href must be what the zone actually serves.
			'/explorer/',
			'/annotator/',
			'/models/',
			'/studio/',
		]);
		// Pinned as absences, because these three leaving the zone bar IS the ruling.
		for (const title of ['Home', 'Projects', 'Settings']) {
			expect(topNav(true).map((e) => e.title)).not.toContain(title);
		}
	});

	it('is IDENTICAL for an estate admin — inside a project, admin earns COLUMNS, not an entry', () => {
		// Restored to the original rule, and it is true again for the right reason. It briefly failed
		// when Settings joined this bar; the two-level ruling moved Settings to the MAIN MENU, so the
		// in-project bar is once more identity-independent — privilege shows up only INSIDE a panel
		// (Lakehouse's Operations column), never as a new destination in the row.
		expect(topNav(true).map((e) => e.title)).toEqual(topNav(false).map((e) => e.title));
		expect(topNav(true).map((e) => e.href)).toEqual(topNav(false).map((e) => e.href));
	});

	it('gates OPERATIONS behind estate-admin inside the Lakehouse panel — governance is not there', () => {
		const labels = (admin: boolean) =>
			topNav(admin)
				.find((e) => e.title === 'Lakehouse')!
				.groups!.map((g) => g.label);
		// The governance guarantee, both polarities: a non-admin's panel cannot even name them.
		// 'Models' left this list when the registry, experiments and pipeline routes physically moved
		// to the MODELS zone — the column went with its routes.
		expect(labels(false)).toEqual(['Workspace', 'Catalog', 'Lineage']);
		// Operations — streams, events, dead letters — is an operation ON the lakehouse, so it stays a
		// column here. Governance is not a lakehouse feature and no longer appears in this panel at
		// all; asserted as an ABSENCE so it cannot quietly return and exist in two places. Models is
		// pinned the same way, for the same reason.
		expect(labels(true)).toEqual(['Workspace', 'Catalog', 'Lineage', 'Operations']);
		expect(labels(true)).not.toContain('Governance');
		expect(labels(true)).not.toContain('Models');
	});

	it('never exposes the access workbench as a navbar entry — it is one row of the Settings panel', () => {
		// A ROW, never a top-level entry: the invariant is unchanged across two moves. The panel holding
		// it moved first (Lakehouse's Governance column → the main menu's Settings entry), then the PAGE
		// followed (#105: /lakehouse/governance/access → /settings/access), and the row was relabelled
		// to what it manages — platform-wide users & roles, not one zone's per-object grants.
		for (const admin of [false, true]) {
			for (const title of ['Access', 'Users & roles']) {
				expect(topNav(admin).map((e) => e.title)).not.toContain(title);
				expect(mainMenuNav(admin).map((e) => e.title)).not.toContain(title);
			}
		}
		const settings = mainMenuNav(true).find((e) => e.title === 'Settings')!;
		expect(settings.items!.find((i) => i.title === 'Users & roles')?.href).toBe('/settings/access');
		// …and a non-admin gets no row carrying it at all, in EITHER bar. Both addresses are pinned: the
		// old one so a resurrected link fails here, the new one so the fail-closed rule still bites.
		const rowsOf = (entries: ReturnType<typeof topNav>) =>
			entries.flatMap((e) => [...(e.items ?? []), ...(e.groups ?? []).flatMap((g) => g.items)]);
		for (const entries of [topNav(false), mainMenuNav(false)]) {
			expect(rowsOf(entries).map((i) => i.href)).not.toContain('/settings/access');
			expect(rowsOf(entries).map((i) => i.href)).not.toContain('/lakehouse/governance/access');
		}
	});

	it('the MAIN MENU carries Home, Projects and Settings — and no zone', () => {
		// The estate level. Standing here you are choosing WHAT to work on, not moving between zones,
		// so the zone bar would answer a question nobody has asked yet.
		expect(mainMenuNav(true).map((e) => e.title)).toEqual(['Home', 'Projects', 'Settings']);
		// A non-admin gets two, not a disabled third — Settings is ABSENT, fail-closed.
		expect(mainMenuNav(false).map((e) => e.title)).toEqual(['Home', 'Projects']);
		expect(mainMenuNav(true).map((e) => e.href)).toEqual(['/', '/projects', '/settings']);
		// No zone leaks in, in either identity — this is what catches a zone added to `topNav` and
		// silently appearing at the estate root too.
		for (const admin of [false, true]) {
			const titles = mainMenuNav(admin).map((e) => e.title);
			for (const zone of topNav(admin).map((e) => e.title)) {
				expect(titles).not.toContain(zone);
			}
		}
	});

	it('Home matches ONLY the root — its siblings are not routes underneath it', () => {
		const home = mainMenuNav(false).find((e) => e.title === 'Home')!;
		expect(home.match('/')).toBe(true);
		// The whole reason it is `exact` and not `under`: at this level Projects and Settings sit BESIDE
		// Home, so an `under('/')` matcher would light Home on every page in the estate.
		for (const p of ['/projects', '/projects/acme', '/settings', '/lakehouse/catalog']) {
			expect(home.match(p)).toBe(false);
		}
	});

	it('closes the ruling: Settings is a REAL route, carries the platform rows, and is admin-ONLY', () => {
		// Fail-closed like the column it replaced: ABSENT for a non-admin rather than
		// present-and-disabled, so the bar never names a surface the viewer is barred from.
		expect(mainMenuNav(false).some((e) => e.title === 'Settings')).toBe(false);
		const menu = mainMenuNav(true);
		expect(menu.at(-1)!.title).toBe('Settings');
		const settings = menu.find((e) => e.title === 'Settings')!;
		// It points at its OWN route, and so does every row of its panel — #105 moved the pages into this
		// app, so the entry, the panel and the routes are finally one thing. The row TITLES are pinned
		// against `/settings`'s own page, which renders the same three under the same names; two controls
		// answering one question must not drift.
		expect(settings.href).toBe('/settings');
		expect(settings.items!.map((i) => i.title)).toEqual(['Users & roles', 'Projects', 'Audit']);
		expect(settings.items!.map((i) => i.href)).toEqual([
			'/settings/access',
			'/projects',
			'/settings/audit',
		]);
		// It lights on its own subtree and nothing else. The matcher also covered `/lakehouse/governance`
		// while those pages were served there; that segment no longer exists in any zone, and a matcher
		// for a path nothing serves can only ever be wrong.
		for (const p of [
			'/settings',
			'/settings/notifications',
			'/settings/access',
			'/settings/audit',
		]) {
			expect(settings.match(p)).toBe(true);
		}
		expect(settings.match('/lakehouse/governance/access')).toBe(false);
		// …and never steals the highlight from a zone. `/projects` is a ROW of this panel but its own
		// main-menu entry owns the highlight, so Settings must stay dark on it.
		expect(settings.match('/lakehouse/catalog')).toBe(false);
		expect(settings.match('/projects')).toBe(false);
	});

	it('isMainMenu splits the two levels — /projects is the list, /projects/<id> is inside one', () => {
		// The predicate the shell swaps bars on. The boundary that matters is the last pair: opening a
		// project is what puts you inside one, so its page gets the ZONE bar while the list above it
		// does not.
		for (const p of ['/', '/projects', '/settings', '/settings/notifications']) {
			expect(isMainMenu(p)).toBe(true);
		}
		for (const p of ['/projects/acme', '/lakehouse/catalog', '/compute/', '/explorer/']) {
			expect(isMainMenu(p)).toBe(false);
		}
		// Trailing-slash robust, because a zone base serves the trailing form.
		expect(isMainMenu('/projects/')).toBe(true);
	});

	it('active-match: Lakehouse lights across every area of its zone, lineage included', () => {
		// One trigger owns the whole zone, so there is no subtree to subtract and no pair of entries
		// that can light up together.
		const lakehouse = topNav(true).find((e) => e.title === 'Lakehouse')!;
		for (const p of [
			'/lakehouse/catalog',
			'/lakehouse/catalog/tables/db$t',
			'/lakehouse/catalog/storage',
			'/lakehouse/admin/events',
		]) {
			expect(lakehouse.match(p)).toBe(true);
		}
		// `/lakehouse/governance/*` is deliberately absent from that list rather than merely unlisted:
		// the segment left this zone with #105, so the zone that lights for it is home's Settings entry.
		expect(lakehouse.match('/settings/audit')).toBe(false);
		expect(lakehouse.match('/lakehouse/lineage')).toBe(true);
		expect(lakehouse.match('/lakehouse/lineage/runs')).toBe(true);
		expect(lakehouse.match('/')).toBe(false);
		expect(lakehouse.match('/explorer')).toBe(false);
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

	it('Explorer and Annotate are separate zones, so separate triggers', () => {
		// One trigger per zone: the annotator is its own microfrontend, so it gets its own entry
		// rather than hiding as a row in the Explorer's panel. Neither trigger claims the other's zone.
		const search = topNav(false).find((e) => e.title === 'Explorer')!;
		const annotate = topNav(false).find((e) => e.title === 'Annotate')!;
		expect(search.match('/explorer')).toBe(true);
		expect(search.match('/annotator')).toBe(false);
		expect(annotate.match('/annotator')).toBe(true);
		expect(annotate.match('/explorer')).toBe(false);
		// Annotate PANELS since #113: Canvas + Browse are two real destinations, and the estate rule
		// is that every multi-surface zone panels (Studio is the one remaining plain link).
		expect(annotate.items?.map((i) => i.title)).toEqual(['Canvas', 'Browse']);
		expect(annotate.groups).toBeUndefined();
		// …and Annotate is no longer buried inside Search's panel.
		expect(search.items?.some((i) => i.href === '/annotator')).toBe(false);
	});

	it('Projects sits in the MAIN MENU — the top of the hierarchy, and not a second Home', () => {
		// There is one project concept, it is the top of the hierarchy, and its list/overview/create
		// surfaces are main-menu pages in the home zone. It reads from `mainMenuNav` now, not `topNav`:
		// the two-level ruling took it out of the in-project bar, where it was the odd non-zone entry.
		// A plain link (one surface plus its per-project detail — a one-row dropdown would be noise),
		// matching the whole subtree so `/projects/<p>` keeps it lit.
		for (const admin of [false, true]) {
			const projects = mainMenuNav(admin).find((e) => e.title === 'Projects')!;
			expect(projects.href).toBe('/projects');
			expect(projects.items).toBeUndefined();
			expect(projects.groups).toBeUndefined();
			expect(projects.match('/projects')).toBe(true);
			expect(projects.match('/projects/acme')).toBe(true);
			// The origin root is HOME, a different entry beside this one — Projects must never claim it,
			// or the main menu carries the same destination twice under two names.
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
		expect(compute.match('/explorer')).toBe(false);
		expect(compute.match('/')).toBe(false);
		// The overview IS the zone root (like Media's Search), so the first row carries entry.href
		// and the panel never prepends a second zone-root row.
		expect(compute.items!.map((i) => i.title)).toEqual([
			'Overview',
			// The zone's own dock, second — `dock-reachability.test.ts` requires /compute/workbench to
			// appear in this panel, because the navbar is how someone standing in another zone learns
			// the dock exists at all.
			'Workbench',
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

	it('Models carries a panel now; Studio is still the one single-surface zone', () => {
		const models = topNav(false).find((e) => e.title === 'Models')!;
		const studio = topNav(false).find((e) => e.title === 'Studio')!;
		// This pair used to be 'Train and Studio are single-surface zones (R17)', on the reasoning that
		// a one-row dropdown is noise. That was true while the zone was four placeholder training
		// pages. R17's migration then actually landed — the lakehouse's registry, experiments and
		// pipeline moved in and the playground joined them — so the zone has real destinations and
		// earns rows. Studio is still the sandbox and still a plain link.
		expect(models.items!.map((i) => i.title)).toEqual([
			'Registry',
			'Experiments',
			'Pipeline',
			'Playground',
			'Training runs',
		]);
		expect(models.groups).toBeUndefined();
		expect(studio.items).toBeUndefined();
		expect(studio.groups).toBeUndefined();
		// The zone root is the REGISTRY, so the trigger's own href is the root and its match spans the
		// whole zone — training is an area under it, not the thing the zone is.
		expect(models.href).toBe('/models/');
		expect(models.match('/models')).toBe(true);
		expect(models.match('/models/submit')).toBe(true);
		expect(models.match('/models/playground')).toBe(true);
		expect(models.match('/studio')).toBe(false);
		expect(studio.match('/studio')).toBe(true);
		expect(studio.match('/studio/animation')).toBe(true);
		expect(studio.match('/models')).toBe(false);
		// The routes left the lakehouse, so the lakehouse trigger must NOT claim them and the models
		// trigger must. Both directions, because a half-done rename lights up two triggers at once.
		const lakehouse = topNav(false).find((e) => e.title === 'Lakehouse')!;
		expect(lakehouse.match('/models')).toBe(false);
		expect(lakehouse.match('/models/pipeline')).toBe(false);
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
		// NO 'Models' COLUMN any more — the registry, experiments and pipeline routes physically moved
		// to the MODELS zone, which is its own trigger with its own panel (asserted below). Pinned as an
		// ABSENCE for the same reason Governance is: a trigger that keeps advertising another zone's
		// routes is how the bar comes to describe an estate that no longer exists.
		expect(groups.Models).toBeUndefined();
		// No Governance column here any more — it is the Settings entry's panel (2026-08-03 ruling), and
		// since #105 the PAGES are home's too. Asserted in full by 'closes the ruling: Settings carries
		// the platform rows…' above. Pinned as an absence so the rows cannot come to exist in both places.
		expect(groups.Governance).toBeUndefined();
		expect(groups.Operations).toEqual(['Events', 'Streams', 'DLQ']);
		expect(groups.Lineage).toEqual(['Datasets', 'Jobs', 'Runs', 'Columns', 'Graph']);
	});

	it('Explorer carries its rows too — the panel that was never pinned', () => {
		// Compute's rows and all five Lakehouse columns are asserted above; the EXPLORER's never were,
		// so its panel could gain or lose a row with this suite still green. That omission had teeth:
		// the explorer is the estate's ONE dock zone, `dock-reachability.test.ts` requires
		// /explorer/workbench to appear here, and nothing in this file would have noticed it go missing.
		const explorer = topNav(false).find((e) => e.title === 'Explorer')!;
		expect(explorer.items!.map((i) => i.title)).toEqual([
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
		// Registry (=/models) sits at its ZONE root; `seg` would keep it lit on every sibling area.
		const m = exact('/models');
		expect(m('/models')).toBe(true);
		expect(m('/models/pipeline')).toBe(false);
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
		expect(zoneOf('/explorer/atlas')).toBe('explorer');
		expect(zoneOf('/')).toBe('');
		expect(zoneOf('')).toBe('');
	});

	it("zoneOf: the home zone's OWN routes are the home zone, not zones of their own", () => {
		// `home` has no base path, so "first segment = zone" misread its own routes as zones: `/projects`
		// looked like a `projects` zone. The consequence was one-directional and easy to miss — a link
		// from the lakehouse still hard-navigated (correct), while standing ON `/` the navbar's own
		// Projects link cost a full document load to reach a route this very app serves.
		for (const p of ['/projects', '/projects/acme', '/settings', '/settings/notifications']) {
			expect(zoneOf(p)).toBe('');
		}
		// Same-zone from home (soft nav), cross-zone from anywhere else (hard nav) — both directions,
		// because getting only one of them right is what the old behaviour did.
		expect(zoneOf('/projects') === zoneOf('/')).toBe(true);
		expect(zoneOf('/projects') === zoneOf('/lakehouse/catalog')).toBe(false);
		// …and it must not swallow a zone that merely SHARES a prefix with a home route.
		expect(zoneOf('/projectsomething')).toBe('projectsomething');
		expect(zoneOf('/settingsx/thing')).toBe('settingsx');
	});
});
