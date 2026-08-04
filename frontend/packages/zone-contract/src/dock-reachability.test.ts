import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, svelteBase, zoneDirs } from './manifest';

/**
 * A DOCK YOU CANNOT REACH FROM ANOTHER ZONE IS NOT SHIPPED.
 *
 * `nav-truth.test.ts` already checks the FORWARD direction — every nav href resolves to a real route,
 * so the rail never promises a page that does not exist. Nothing checked the REVERSE, and that is the
 * direction this estate keeps failing in:
 *
 *   * R28 — the S3 object browser was reachable ONLY by typing the URL. The sidebar is area-scoped, so
 *     no other area linked to it and the navbar panel never listed it.
 *   * The three dock workbenches, 2026-07-29 — each zone's own sidebar carried its Workbench from the
 *     day the dock landed, so a forward check was green, while the shared navbar panel listed every
 *     other area of those zones and never the dock. From compute, the lineage workbench took two hops
 *     and prior knowledge that it existed.
 *
 * Both are the same defect: a surface exists, its own zone knows about it, and the estate does not.
 *
 * **`nav-config.ts` had NO test of any kind before this file.** The shared top navbar is where R15
 * lives ("a zone missing from the shared navbar is a defect, regardless of scaffold status") and it
 * was the one navigation surface no gate read. That is why this checks the navbar and not only the
 * sidebar — the sidebar was never the half that was broken.
 *
 * Scoped to DOCKS rather than to all routes on purpose. "Every route must appear in the nav" is a rule
 * this estate would be right to reject: detail pages reached from a table, `[id]` routes, and the
 * `capi/*` BFF endpoints are all legitimately unlisted. Docks are a small, named, deliberately-built
 * set that costs ~100 KB gzipped each — if one is not worth a navbar row, it was not worth building.
 */

/** `import('@rask/dockview')` or `from '@rask/dockview'` — either spelling mounts a dock. */
const MOUNTS_DOCK = /['"]@rask\/dockview['"]/;

const NAV_CONFIG = resolve(FRONTEND_ROOT, 'packages', 'ui', 'src', 'lib', 'shell', 'nav-config.ts');

/** Every `+page.svelte` under a zone's routes tree, as [absolute path, route segments]. */
function pagesOf(zone: string): { file: string; segments: string[] }[] {
	const root = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'routes');
	const out: { file: string; segments: string[] }[] = [];
	if (!existsSync(root)) return out;

	const walk = (dir: string, segments: string[]): void => {
		for (const entry of readdirSync(dir, { withFileTypes: true })) {
			if (entry.isDirectory()) {
				// SvelteKit route GROUPS `(name)` organise files without contributing a URL segment.
				const isGroup = entry.name.startsWith('(') && entry.name.endsWith(')');
				walk(resolve(dir, entry.name), isGroup ? segments : [...segments, entry.name]);
			} else if (entry.name === '+page.svelte') {
				out.push({ file: resolve(dir, entry.name), segments });
			}
		}
	};
	walk(root, []);
	return out;
}

/** The docks the estate actually ships, discovered from source rather than from a list to keep. */
function dockRoutes(): { zone: string; href: string; file: string }[] {
	return zoneDirs().flatMap((zone) => {
		const base = svelteBase(zone);
		return pagesOf(zone)
			.filter(({ file }) => MOUNTS_DOCK.test(readFileSync(file, 'utf8')))
			.map(({ segments, file }) => ({
				zone,
				href: `${base}/${segments.join('/')}`.replace(/\/+/g, '/'),
				file,
			}));
	});
}

const DOCKS = dockRoutes();

describe('dock reachability', () => {
	it('pins the docks the estate ships — one per zone that has one, all navigable', () => {
		// 2026-08-03 (final ruling): docks belong INSIDE the zones and the global compositor is
		// RETIRED (docs/architecture/global-workbench.md). A zone's dock composes that zone's own
		// components over one shared store — full fidelity, no cross-zone transport. The pin stays
		// EXACT: every dock the estate ships must be navigable, and a compositor cannot come back
		// unnoticed. ONE zone ships one today — media, where "cut and re-cut the corpus" is the
		// workflow that wanted it. Others earn a dock when a real multi-panel workflow appears in
		// them, not by symmetry.
		expect(DOCKS.map((d) => d.href).sort()).toEqual(['/explorer/workbench']);
	});

	it.each(DOCKS)('$href is listed in the $zone zone sidebar', ({ zone, href }) => {
		const nav = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'lib', 'nav.ts');
		expect(existsSync(nav), `${zone} has a dock but no src/lib/nav.ts`).toBe(true);
		expect(
			readFileSync(nav, 'utf8'),
			`${href} mounts a dock but ${zone}/src/lib/nav.ts does not link to it — the zone's own ` +
				'sidebar is the ONE place a visitor already inside the zone can find it',
		).toContain(`'${href}'`);
	});

	it.each(DOCKS)('$href is listed in the shared top navbar', ({ href }) => {
		// The navbar panel is the CROSS-zone jump list: it is how someone standing in compute learns the
		// lineage workbench exists at all. A dock present only in its own zone's sidebar is reachable in
		// two hops by someone who already knows to look — which is the R28 shape, not reachability.
		//
		// If a future dock lands in a zone that has no navbar panel, the fix is to give that zone a
		// panel (R15's whole point), NOT to relax this into "…unless the zone has no panel". That escape
		// hatch is how the surface would go quiet again.
		expect(
			readFileSync(NAV_CONFIG, 'utf8'),
			`${href} mounts a dock but @rask/ui's nav-config.ts never names it, so it cannot be reached ` +
				'from another zone in one hop',
		).toContain(`'${href}'`);
	});
});
