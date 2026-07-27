import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { findViolations, isCrossZonePath, ZONES } from './cross-zone-reload';
import { FRONTEND_ROOT, zoneDirs } from './manifest';
import { globSync } from 'node:fs';

// Cross-zone hrefs are single-segment, domain-relative (`/lakehouse/data/tables`, `/media`).
// The guard predicate must match the current scheme or it silently protects nothing.
describe('isCrossZonePath', () => {
	it('matches a nested cross-zone path', () => {
		expect(isCrossZonePath('/lakehouse/data/tables')).toBe(true);
		expect(isCrossZonePath('/media/atlas')).toBe(true);
	});

	it('matches a bare zone landing', () => {
		expect(isCrossZonePath('/lakehouse')).toBe(true);
		expect(isCrossZonePath('/annotator')).toBe(true);
	});

	it('does NOT match a hop BETWEEN areas of the merged lakehouse zone', () => {
		// data / lineage / models / admin used to be four zones, so a link between them had to
		// hard-navigate. They are one zone now: these are same-zone soft navs, and flagging them would
		// force a full document reload where the router can handle it.
		expect(isCrossZonePath('/data/tables')).toBe(false);
		expect(isCrossZonePath('/lineage')).toBe(false);
		expect(isCrossZonePath('/models/experiments')).toBe(false);
		expect(isCrossZonePath('/admin/dlq')).toBe(false);
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

	it('names every zone that has a base path, and only those', () => {
		// The predicate is a hardcoded list; if a zone is added or renamed and this is not, the gate
		// stops protecting the new zone without saying so.
		expect([...ZONES].sort()).toEqual(zoneDirs().filter((z) => z !== 'home'));
	});
});

describe('findViolations reads the markup, not a regex', () => {
	it('flags a bare cross-zone link', () => {
		expect(findViolations('<a href="/media/atlas">atlas</a>')).toEqual([
			{ href: '/media/atlas', line: 1 },
		]);
	});

	it('accepts one that hard-navigates', () => {
		expect(findViolations('<a href="/media/atlas" data-sveltekit-reload>atlas</a>')).toEqual([]);
		expect(findViolations('<a href="/media" data-sveltekit-reload="">m</a>')).toEqual([]);
	});

	it('flags data-sveltekit-reload="off", which disables it', () => {
		expect(findViolations('<a href="/media" data-sveltekit-reload="off">m</a>')).toHaveLength(1);
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
		expect(findViolations('<p>a</p>\n<p>b</p>\n<a href="/media">m</a>')[0]?.line).toBe(3);
	});
});

describe('every cross-zone link in the estate hard-navigates', () => {
	// The gate itself. A soft nav into another zone resolves against a route manifest that does not
	// contain the target — a 404 that type-checks, unit-tests and renders fine.
	const components = globSync('microfrontends/*/src/**/*.svelte', { cwd: FRONTEND_ROOT });

	it('finds components to check', () => {
		expect(components.length).toBeGreaterThan(100);
	});

	it.each(components)('%s', (rel) => {
		const found = findViolations(readFileSync(resolve(FRONTEND_ROOT, rel), 'utf8'));
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
