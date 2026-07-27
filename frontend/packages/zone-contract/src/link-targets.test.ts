import { globSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { findDeadLinks, servedSegments } from './link-targets';
import { FRONTEND_ROOT } from './manifest';

describe('the served set is derived from the zones, not hardcoded', () => {
	it('contains every zone prefix and the catch-all zone routes', () => {
		const served = servedSegments();
		for (const zone of ['lakehouse', 'media', 'annotator']) {
			expect(served, `${zone} serves /${zone}`).toContain(zone);
		}
		// home owns no prefix, so its own route dirs are reachable at the root.
		expect(served).toContain('auth');
		expect(served).toContain('capi');
		// And the merged-away pre-merge zone paths are NOT served — the whole point.
		for (const gone of ['data', 'lineage', 'models', 'admin']) {
			expect(
				served,
				`/${gone} was a pre-merge zone root; it is under /lakehouse now`,
			).not.toContain(gone);
		}
	});
});

describe('findDeadLinks', () => {
	const served = new Set(['lakehouse', 'media', 'annotator', 'auth', 'capi']);

	it('flags a link to a merged-away zone root', () => {
		const found = findDeadLinks('<a href="/data" data-sveltekit-reload>x</a>', served);
		expect(found).toEqual([{ href: '/data', line: 1, segment: 'data' }]);
	});

	it('flags it even WITH data-sveltekit-reload — that is what made it invisible', () => {
		// All five real ones hard-navigated correctly. To a 404.
		expect(findDeadLinks('<a href="/lineage" data-sveltekit-reload>x</a>', served)).toHaveLength(1);
	});

	it('flags a deep path whose FIRST segment is unserved', () => {
		const found = findDeadLinks('<a href={`/data/projects/${p}`}>x</a>', served);
		expect(found[0]?.segment).toBe('data');
	});

	it('accepts a served zone path, at the root or deep', () => {
		expect(findDeadLinks('<a href="/lakehouse">x</a>', served)).toEqual([]);
		expect(findDeadLinks('<a href="/lakehouse/data/projects/x">y</a>', served)).toEqual([]);
	});

	it('ignores same-zone `{base}/…` links — they never carry a zone prefix', () => {
		expect(findDeadLinks('<a href="{base}/data/tables">x</a>', served)).toEqual([]);
	});

	it('ignores external, hash, and query-only hrefs', () => {
		expect(findDeadLinks('<a href="https://example.com/data">x</a>', served)).toEqual([]);
		expect(findDeadLinks('<a href="#section">x</a>', served)).toEqual([]);
		expect(findDeadLinks('<a href="?tab=1">x</a>', served)).toEqual([]);
	});

	it('ignores "/" itself — the catch-all zone serves it', () => {
		expect(findDeadLinks('<a href="/">home</a>', served)).toEqual([]);
	});

	it('does not guess at a dynamic first segment', () => {
		expect(findDeadLinks('<a href={`/${zone}/thing`}>x</a>', served)).toEqual([]);
	});

	it('reports the line', () => {
		expect(findDeadLinks('<p>a</p>\n<p>b</p>\n<a href="/data">d</a>', served)[0]?.line).toBe(3);
	});
});

describe('every domain-relative link in the estate lands somewhere', () => {
	// The file set includes packages/**, which the reload gate does NOT scan — and that omission is
	// exactly how the project switcher shipped a dead `/data` in the shell of all four zones.
	const components = [
		...globSync('microfrontends/*/src/**/*.svelte', { cwd: FRONTEND_ROOT }),
		...globSync('packages/*/src/**/*.svelte', { cwd: FRONTEND_ROOT }),
	];
	const served = servedSegments();

	it('scans the zones AND the shared packages', () => {
		expect(components.length).toBeGreaterThan(100);
		expect(
			components.some((f) => f.startsWith('packages/')),
			'the shared shell lives in packages/ui — a gate that skips it cannot see the switcher',
		).toBe(true);
	});

	it.each(components)('%s', (rel) => {
		const found = findDeadLinks(readFileSync(resolve(FRONTEND_ROOT, rel), 'utf8'), served);
		expect(
			found,
			found
				.map(
					(v) =>
						`${rel}:${v.line} links to "${v.href}" but no zone serves "/${v.segment}" — a hard ` +
						`navigation straight to a 404. Zone paths are /lakehouse, /media, /annotator; the ` +
						`pre-merge roots (/data, /lineage, /models, /admin) are areas of the lakehouse zone now.`,
				)
				.join('\n'),
		).toEqual([]);
	});
});
