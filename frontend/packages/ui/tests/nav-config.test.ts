import { describe, expect, it } from 'vitest';
import {
	exact,
	isMainMenu,
	mainMenuNav,
	norm,
	seg,
	topNav,
	under,
} from '../src/lib/shell/nav-config';

// The top-navbar IA and the matchers the sidebar config is built with. Two bars: the MAIN MENU at the
// estate level (Home, Projects, Settings) and the in-project bar, which is the Lakehouse entry with its
// workspace, catalog and (estate-admin only) operations as columns of one panel.
describe('topNav', () => {
	it('is the IN-PROJECT bar: the Lakehouse entry alone, and none of the estate level', () => {
		expect(topNav(false).map((e) => e.title)).toEqual(['Lakehouse']);
		expect(topNav(false)[0]!.href).toBe('/catalog');
	});

	it('is IDENTICAL for an estate admin — inside a project, admin earns COLUMNS, not an entry', () => {
		expect(topNav(true).map((e) => e.title)).toEqual(topNav(false).map((e) => e.title));
		expect(topNav(true).map((e) => e.href)).toEqual(topNav(false).map((e) => e.href));
	});

	it('gates OPERATIONS behind estate-admin inside the Lakehouse panel — governance is not there', () => {
		const labels = (admin: boolean) =>
			topNav(admin)
				.find((e) => e.title === 'Lakehouse')!
				.groups!.map((g) => g.label);
		// The governance guarantee, both polarities: a non-admin's panel cannot even name them.
		expect(labels(false)).toEqual(['Workspace', 'Catalog']);
		// Operations — streams, events, dead letters — is an operation ON the lakehouse, so it stays a
		// column here. Governance is not a lakehouse feature and no longer appears in this panel at
		// all; asserted as an ABSENCE so it cannot quietly return and exist in two places.
		expect(labels(true)).toEqual(['Workspace', 'Catalog', 'Operations']);
		expect(labels(true)).not.toContain('Governance');
	});

	it('never exposes the access workbench as a navbar entry — it is one row of the Settings panel', () => {
		// A ROW, never a top-level entry: the invariant is unchanged across two moves. The panel holding
		// it moved first (Lakehouse's Governance column → the main menu's Settings entry), then the PAGE
		// followed (#105: → /settings/access), and the row was relabelled to what it manages —
		// platform-wide users & roles.
		for (const admin of [false, true]) {
			for (const title of ['Access', 'Users & roles']) {
				expect(topNav(admin).map((e) => e.title)).not.toContain(title);
				expect(mainMenuNav(admin).map((e) => e.title)).not.toContain(title);
			}
		}
		const settings = mainMenuNav(true).find((e) => e.title === 'Settings')!;
		expect(settings.items!.find((i) => i.title === 'Users & roles')?.href).toBe('/settings/access');
		// …and a non-admin gets no row carrying it at all, in EITHER bar.
		const rowsOf = (entries: ReturnType<typeof topNav>) =>
			entries.flatMap((e) => [...(e.items ?? []), ...(e.groups ?? []).flatMap((g) => g.items)]);
		for (const entries of [topNav(false), mainMenuNav(false)]) {
			expect(rowsOf(entries).map((i) => i.href)).not.toContain('/settings/access');
		}
	});

	it('the MAIN MENU carries Home, Projects and Settings — and not the Lakehouse', () => {
		// The estate level. Standing here you are choosing WHAT to work on.
		expect(mainMenuNav(true).map((e) => e.title)).toEqual(['Home', 'Projects', 'Settings']);
		// A non-admin gets the SAME THREE, the third disabled with its reason (#143). This asserted
		// `['Home', 'Projects']` — Settings ABSENT — which the ruling overturns: a bar that loses the
		// entry tells a non-admin the estate has no settings rather than that they cannot open them.
		const denied = mainMenuNav(false);
		expect(denied.map((e) => e.title)).toEqual(['Home', 'Projects', 'Settings']);
		const deniedSettings = denied.find((e) => e.title === 'Settings')!;
		expect(deniedSettings.unavailable).toMatch(/can_observe_events/);
		// Disabled means CHILDLESS. The entry is shown; its rows are not, and not merely unrendered —
		// they are absent from the data, so no future branch-ordering edit in the navbar can leak them.
		expect(deniedSettings.items).toBeUndefined();
		// The reason is caller-supplied when the identity is unresolved: "ask for can_observe_events" is
		// useless advice to someone who is not signed in, and the point of the reason is the next step.
		expect(
			mainMenuNav(false, 'sign in first').find((e) => e.title === 'Settings')!.unavailable,
		).toBe('sign in first');
		expect(mainMenuNav(true).map((e) => e.href)).toEqual(['/', '/projects', '/settings']);
		// No in-project entry leaks in, in either identity.
		for (const admin of [false, true]) {
			const titles = mainMenuNav(admin).map((e) => e.title);
			for (const entry of topNav(admin).map((e) => e.title)) {
				expect(titles).not.toContain(entry);
			}
		}
	});

	it('Home matches ONLY the root — its siblings are not routes underneath it', () => {
		const home = mainMenuNav(false).find((e) => e.title === 'Home')!;
		expect(home.match('/')).toBe(true);
		// The whole reason it is `exact` and not `under`: at this level Projects and Settings sit BESIDE
		// Home, so an `under('/')` matcher would light Home on every page in the estate.
		for (const p of ['/projects', '/projects/acme', '/settings', '/catalog']) {
			expect(home.match(p)).toBe(false);
		}
	});

	it('closes the ruling: Settings is a REAL route, carries the platform rows, and is admin-ONLY', () => {
		// Fail-closed like the column it replaced — but PRESENT-AND-DISABLED for a non-admin since #143,
		// not absent. The door is what stays closed; the bar's job is to say the surface exists and why
		// this viewer cannot open it. "Never names a surface the viewer is barred from" was the old rule
		// and is exactly what the ruling reverses.
		const deniedEntry = mainMenuNav(false).find((e) => e.title === 'Settings');
		expect(deniedEntry).toBeDefined();
		expect(deniedEntry!.unavailable).toBeTruthy();
		// Fail-closed is preserved where it counts: no rows, so the panel cannot be opened into.
		expect(deniedEntry!.items).toBeUndefined();
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
		// It lights on its own subtree and nothing else.
		for (const p of ['/settings', '/settings/access', '/settings/audit']) {
			expect(settings.match(p)).toBe(true);
		}
		// …and never steals the highlight. `/projects` is a ROW of this panel but its own
		// main-menu entry owns the highlight, so Settings must stay dark on it.
		expect(settings.match('/catalog')).toBe(false);
		expect(settings.match('/projects')).toBe(false);
	});

	it('isMainMenu splits the two levels — /projects is the list, /projects/<id> is inside one', () => {
		// The predicate the shell swaps bars on: opening a project is what puts you inside one, so its
		// page gets the in-project bar while the list above it does not.
		for (const p of ['/', '/projects', '/settings', '/settings/access']) {
			expect(isMainMenu(p)).toBe(true);
		}
		for (const p of ['/projects/acme', '/catalog', '/admin/events', '/workbench']) {
			expect(isMainMenu(p)).toBe(false);
		}
		// Trailing-slash robust.
		expect(isMainMenu('/projects/')).toBe(true);
	});

	it('active-match: Lakehouse lights across every lakehouse page and nowhere else', () => {
		const lakehouse = topNav(true).find((e) => e.title === 'Lakehouse')!;
		for (const p of [
			'/catalog',
			'/catalog/tables/db$t',
			'/catalog/storage',
			'/admin/events',
			'/workbench',
		]) {
			expect(lakehouse.match(p)).toBe(true);
		}
		for (const p of ['/settings/audit', '/', '/projects', '/projects/acme']) {
			expect(lakehouse.match(p)).toBe(false);
		}
	});

	it('Projects sits in the MAIN MENU — the top of the hierarchy, and not a second Home', () => {
		// There is one project concept, it is the top of the hierarchy, and its list/overview/create
		// surfaces are main-menu pages.
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
			expect(projects.match('/catalog')).toBe(false);
		}
	});

	it('carries NO Home entry — the origin root is the main menu, not the in-project bar', () => {
		for (const admin of [false, true]) {
			expect(topNav(admin).some((e) => e.title === 'Home')).toBe(false);
			expect(topNav(admin).some((e) => e.href === '/')).toBe(false);
		}
	});

	it('carries the expected rows per column', () => {
		const groups = Object.fromEntries(
			topNav(true)
				.find((e) => e.title === 'Lakehouse')!
				.groups!.map((g) => [g.label, g.items.map((i) => i.title)]),
		);
		// HIERARCHY ORDER (project > warehouse > namespace > table), identical to the sidebar's; no
		// Projects row, because a project is the top of the hierarchy and lives in the main menu.
		expect(groups.Catalog).toEqual(['Warehouses', 'Namespaces', 'Tables', 'Storage']);
		// No Governance column: it is the Settings entry's panel (2026-08-03 ruling).
		expect(groups.Governance).toBeUndefined();
		expect(groups.Operations).toEqual(['Events', 'Streams', 'DLQ']);
		expect(groups.Lineage).toBeUndefined();
	});

	it('every entry is reachable: a panel with rows, or a plain link', () => {
		for (const entry of topNav(true)) {
			expect(entry.href.startsWith('/'), `${entry.title} href`).toBe(true);
			const rows = [...(entry.items ?? []), ...(entry.groups ?? []).flatMap((g) => g.items)];
			// A plain link has no rows to check; a panel must not be EMPTY, which would render a trigger
			// that opens onto nothing.
			if (entry.items === undefined && entry.groups === undefined) continue;
			expect(rows.length, `${entry.title} panel is empty`).toBeGreaterThan(0);
			for (const item of rows) {
				expect(item.href.startsWith('/')).toBe(true);
				expect(item.description.length).toBeGreaterThan(0);
			}
		}
	});
});

describe('PageNav matchers', () => {
	it('seg: matches the exact route and anything nested under it', () => {
		const m = seg('/catalog/tables');
		expect(m('/catalog/tables')).toBe(true);
		expect(m('/catalog/tables/x')).toBe(true);
		expect(m('/catalog/namespaces')).toBe(false);
	});

	it('exact: matches only its own path — the root-leaf case', () => {
		// Overview (=/catalog) is a prefix of its siblings; `seg` would keep it lit on every one.
		const m = exact('/catalog');
		expect(m('/catalog')).toBe(true);
		expect(m('/catalog/tables')).toBe(false);
	});

	it('norm + matchers tolerate a trailing slash', () => {
		expect(norm('/catalog/')).toBe('/catalog');
		expect(norm('/')).toBe('/');
		expect(exact('/catalog')('/catalog/')).toBe(true);
		expect(under('/catalog')('/catalog/')).toBe(true);
	});
});
