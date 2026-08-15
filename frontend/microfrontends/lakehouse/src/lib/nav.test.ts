import { describe, expect, it } from 'vitest';
import { lakehouseSidebar } from './nav';
import type { ZoneNav, ZoneNavLeaf } from '@rask/ui/shell';

/** Every leaf the rail renders, parents and children alike.
 *
 *  Children are walked because the estate has already shipped the bug where they were not: a shallow
 *  copy disabled a parent and left its sub-routes live links into the same refused surface. */
function allLeaves(nav: ZoneNav): ZoneNavLeaf[] {
	return (nav.groups ?? []).flatMap((g) =>
		g.items.flatMap((leaf) => [leaf, ...(leaf.children ?? [])]),
	);
}

describe('lakehouseSidebar — the rail must not contradict the page it frames', () => {
	it('offers no live project-scoped link when no project is open', () => {
		const nav = lakehouseSidebar(true, null);
		const live = allLeaves(nav).filter((leaf) => !leaf.unavailable);
		expect(live.map((leaf) => leaf.href)).toEqual([]);
	});

	it('keeps the root Overview reachable — it is the page that explains the empty state', () => {
		// Disabling this too would leave the rail with nothing reachable and no way to read WHY, which
		// is strictly worse than the dead links it replaced.
		const nav = lakehouseSidebar(true, null);
		expect(nav.root?.href).toBe('/lakehouse');
		expect((nav.root as { unavailable?: string } | undefined)?.unavailable).toBeUndefined();
	});

	it('names the missing context, and points at the control that supplies it', () => {
		const nav = lakehouseSidebar(true, null);
		for (const leaf of allLeaves(nav)) {
			expect(leaf.unavailable).toContain('active project');
		}
	});

	it('restores every link once a project is open', () => {
		const nav = lakehouseSidebar(true, 'bind86');
		expect(allLeaves(nav).filter((leaf) => leaf.unavailable)).toEqual([]);
	});

	it('lets the FGA denial win over the project reason — it is the one that survives picking a project', () => {
		// A non-admin with no project has two problems. Pick a project and Operations is STILL refused,
		// so telling them to pick one would strand them: they would fix the named cause and watch the
		// row stay dead. The harder gate has to be the one that speaks.
		const nav = lakehouseSidebar(false, null);
		const operations = (nav.groups ?? []).find((g) => g.label === 'Operations');
		expect(operations).toBeDefined();
		for (const leaf of operations?.items ?? []) {
			expect(leaf.unavailable).toContain('estate-admin only');
		}
	});

	it('still disables the non-privileged groups for that same non-admin', () => {
		// The corollary of the rule above: precedence must not become an exemption.
		const nav = lakehouseSidebar(false, null);
		const catalog = (nav.groups ?? []).find((g) => g.label === 'Catalog');
		for (const leaf of catalog?.items ?? []) {
			expect(leaf.unavailable).toContain('active project');
		}
	});
});
