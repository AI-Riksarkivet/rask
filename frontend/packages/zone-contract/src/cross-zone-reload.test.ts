import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { findViolations, isCrossZonePath, ZONES } from './cross-zone-reload';
import { FRONTEND_ROOT, zoneDirs } from './manifest';
import { globSync } from 'node:fs';

// Cross-zone hrefs are single-segment, domain-relative (`/lakehouse/catalog/tables`, `/explorer`).
// The guard predicate must match the current scheme or it silently protects nothing.
describe('isCrossZonePath', () => {
	it('matches a nested cross-zone path', () => {
		expect(isCrossZonePath('/lakehouse/catalog/tables')).toBe(true);
		expect(isCrossZonePath('/explorer/atlas')).toBe(true);
	});

	it('matches a bare zone landing', () => {
		expect(isCrossZonePath('/lakehouse')).toBe(true);
		expect(isCrossZonePath('/annotator')).toBe(true);
	});

	it('does NOT match a hop BETWEEN areas of the merged lakehouse zone', () => {
		// data / lineage / admin used to be separate zones, so a link between them had to
		// hard-navigate. They are one zone now: these are same-zone soft navs, and flagging them would
		// force a full document reload where the router can handle it.
		expect(isCrossZonePath('/data/tables')).toBe(false);
		expect(isCrossZonePath('/lineage')).toBe(false);
		expect(isCrossZonePath('/admin/dlq')).toBe(false);
	});

	it('DOES match /models again — it is a zone once more, not a lakehouse area', () => {
		// `/models/experiments` was asserted above as a same-zone hop, on the reading that models was
		// one of the four pre-merge roots folded under /lakehouse. That reading expired: the model
		// surfaces moved OUT of the lakehouse into a zone of their own, so these are separate
		// SvelteKit apps again and a link between them must hard-navigate or it 404s.
		//
		// This is the exact bug class the gate exists for, arriving from the opposite direction — a
		// segment that stopped being an area and became a zone. The old assertion would have told a
		// lakehouse → models link that it needed no reload.
		expect(isCrossZonePath('/models')).toBe(true);
		expect(isCrossZonePath('/models/experiments')).toBe(true);
		// …and from INSIDE the models zone its own routes stay soft.
		expect(isCrossZonePath('/models/experiments', 'models')).toBe(false);
		expect(isCrossZonePath('/lakehouse/lineage', 'models')).toBe(true);
	});

	it('ignores same-zone / non-zone and opaque-expression hrefs', () => {
		expect(isCrossZonePath('/')).toBe(false);
		expect(isCrossZonePath('/api/runs')).toBe(false);
		// A leading `{base}` expression renders as the opaque placeholder — never a zone.
		expect(isCrossZonePath('￿/tables')).toBe(false);
	});

	it('does NOT match a two-segment /default/<zone> form', () => {
		expect(isCrossZonePath('/default/lakehouse')).toBe(false);
	});

	it("treats the HOME zone's own routes as cross-app from every other zone", () => {
		// The blind spot HOME_ROUTES closes. `home` is the catch-all, so its routes carry no zone
		// prefix and read like same-zone absolute hrefs — but it is a separate SvelteKit app, so a soft
		// nav into it 404s exactly like any zone hop. Only reachable with an owner: without one the
		// gate cannot tell "/projects/x" written IN home from the same href written in the lakehouse.
		expect(isCrossZonePath('/projects', 'lakehouse')).toBe(true);
		expect(isCrossZonePath('/projects/acme', 'explorer')).toBe(true);
		expect(isCrossZonePath('/projects/acme', 'home')).toBe(false);
		expect(isCrossZonePath('/projects/acme')).toBe(false);
		// …and it must not swallow a same-prefix path that is nobody's route.
		expect(isCrossZonePath('/projectsomething', 'lakehouse')).toBe(false);
	});

	it('stops flagging a zone link written INSIDE that same zone, once the owner is known', () => {
		// The other half of knowing the owner, and the reason it is opt-in: a lakehouse component
		// linking to an absolute `/lakehouse/…` path is a same-app soft nav. Forcing a reload there
		// costs a document load on an in-zone hop.
		expect(isCrossZonePath('/lakehouse/catalog', 'lakehouse')).toBe(false);
		expect(isCrossZonePath('/lakehouse/catalog', 'explorer')).toBe(true);
	});

	it('names every zone that has a base path, and only those', () => {
		// The predicate is a hardcoded list; if a zone is added or renamed and this is not, the gate
		// stops protecting the new zone without saying so.
		expect([...ZONES].sort()).toEqual(zoneDirs().filter((z) => z !== 'home'));
	});
});

describe('findViolations reads the markup, not a regex', () => {
	it('flags a bare cross-zone link', () => {
		expect(findViolations('<a href="/explorer/atlas">atlas</a>')).toEqual([
			{ href: '/explorer/atlas', line: 1 },
		]);
	});

	it('accepts one that hard-navigates', () => {
		expect(findViolations('<a href="/explorer/atlas" data-sveltekit-reload>atlas</a>')).toEqual([]);
		expect(findViolations('<a href="/explorer" data-sveltekit-reload="">m</a>')).toEqual([]);
	});

	it('flags data-sveltekit-reload="off", which disables it', () => {
		expect(findViolations('<a href="/explorer" data-sveltekit-reload="off">m</a>')).toHaveLength(1);
	});

	it('ignores a same-zone {base}/… link', () => {
		expect(findViolations('<a href={`${base}/data/tables`}>t</a>')).toEqual([]);
	});

	it('ignores an href it cannot read statically', () => {
		expect(findViolations('<a {...props}>x</a>')).toEqual([]);
		expect(findViolations('<a href={someHref}>x</a>')).toEqual([]);
	});

	it('finds links nested inside blocks and snippets', () => {
		const src = `{#if ok}{#each rows as r (r.id)}<a href="/annotator/{r.id}">go</a>{/each}{/if}`;
		expect(findViolations(src)).toHaveLength(1);
	});

	it('reports the line, so a failure points at the link', () => {
		expect(findViolations('<p>a</p>\n<p>b</p>\n<a href="/explorer">m</a>')[0]?.line).toBe(3);
	});
});

describe('every cross-zone link in the estate hard-navigates', () => {
	// The gate itself. A soft nav into another zone resolves against a route manifest that does not
	// contain the target — a 404 that type-checks, unit-tests and renders fine.
	// @rask/ui is IN the gate (#92): the shell renders in all seven zones, so a shell link to any
	// zone's base is cross-zone from six of them — which is exactly where two defects hid while the
	// glob stopped at microfrontends/*. A shell component has no owning zone, so it gets none: every
	// statically-readable zone-base href in it must hard-navigate, the same rule the `__project`
	// crumb already applies to itself unconditionally.
	const components = [
		...globSync('microfrontends/*/src/**/*.svelte', { cwd: FRONTEND_ROOT }),
		...globSync('packages/ui/src/**/*.svelte', { cwd: FRONTEND_ROOT }),
	];

	it('finds components to check', () => {
		expect(components.length).toBeGreaterThan(100);
	});

	it.each(components)('%s', (rel) => {
		// The OWNING zone comes from the path (`microfrontends/<zone>/src/…`). Passing it is what lets
		// the gate see the home zone's own routes: `/projects` is cross-app from every zone but home,
		// and indistinguishable from a same-zone href without knowing who is asking. A shell component
		// (`packages/ui/…`) has NO owner — it renders in every zone, so no zone-base href is ever
		// safely "same-zone" for it.
		const owner = rel.startsWith('microfrontends/') ? rel.split('/')[1] : undefined;
		const found = findViolations(readFileSync(resolve(FRONTEND_ROOT, rel), 'utf8'), owner);
		expect(
			found,
			found
				.map(
					(v) =>
						`${rel}:${v.line} links to "${v.href}" without data-sveltekit-reload — a soft client ` +
						`nav targets a route this zone does not own (404). Add data-sveltekit-reload, or use ` +
						`{base}/… if it is a same-zone link.`,
				)
				.join('\n'),
		).toEqual([]);
	});
});

describe("the shell's breadcrumb hard-navigates the project crumb from EVERY branch", () => {
	// The gate above cannot see this defect: the breadcrumb anchors render `href={crumb.href}`, a
	// dynamic expression the static scan deliberately ignores. But the crumb list can contain
	// `__project` — a link into the HOME zone — and `collapseCrumbs` puts EVERYTHING in `tail`
	// whenever the trail fits (breadcrumb.ts:102), i.e. the common case. A tail branch without the
	// hard-nav conditional soft-navigates the project crumb from six zones into a route they do not
	// own. Found with two of three branches carrying the conditional; this pins all-or-nothing so a
	// fourth render site cannot reopen the hole.
	const shell = readFileSync(
		resolve(FRONTEND_ROOT, 'packages/ui/src/lib/shell/app-shell.svelte'),
		'utf8',
	);

	it('every crumb.href anchor carries the __project reload conditional', () => {
		const anchors = shell.match(/href=\{crumb\.href\}/g) ?? [];
		const conditionals =
			shell.match(/data-sveltekit-reload=\{crumb\.id === '__project' \? '' : undefined\}/g) ?? [];
		expect(anchors.length).toBeGreaterThan(0);
		expect(
			conditionals.length,
			`${anchors.length} breadcrumb anchor(s) render crumb.href but only ${conditionals.length} ` +
				`carry the __project hard-nav conditional — the uncovered branch soft-navigates the ` +
				`project crumb cross-zone (404).`,
		).toBe(anchors.length);
	});
});
