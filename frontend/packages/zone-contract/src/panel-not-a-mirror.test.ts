import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { FRONTEND_ROOT, zoneDirs } from './manifest';

/**
 * A DOCK PANEL RENDERS A SHARED COMPONENT — it is never a second, smaller re-implementation.
 *
 * This defect class has now appeared twice, which is the repo's own threshold for turning a
 * convention into a gate:
 *
 *   1. The cross-zone compositor. An element could not import a remote function or a `$app`-bound
 *      component, so every panel HAD to be mirrored. That cost is what retired it.
 *   2. Inside the zones, where nothing forced it. `/compute/actors` was 416 lines with sorting,
 *      filters and search; `ActorsPanel` was 49 lines, four columns, no controls, sharing ZERO
 *      components. Same for cluster, jobs, serve and the lakehouse run board — five of twelve
 *      panels, while the record, the skill and the PR all claimed there was no mirroring.
 *
 * The test is deliberately structural rather than visual: a panel must IMPORT a component from its
 * zone's `$lib`. Importing only a `.remote.ts` is the signature of a hand-written panel — it has the
 * data and draws its own table, which is exactly how the two drift.
 *
 * A panel with no page counterpart is legitimate and is NOT a mirror; it just has to say so here.
 */

/** Panels that render data no route renders, so there is no component to share. Each needs a reason. */
const NO_PAGE_COUNTERPART: Record<string, string> = {
	'lakehouse/EventsPanel.svelte':
		'renders LineageState.events (the OpenLineage feed). No route renders it — /admin/events is ' +
		'ControlEventsFeed over the governance plane, a different source. Pairing them would invent a ' +
		'relationship that does not exist.',
};

function panelsOf(zone: string): { file: string; key: string; src: string }[] {
	const dir = resolve(FRONTEND_ROOT, 'microfrontends', zone, 'src', 'lib', 'dock', 'panels');
	if (!existsSync(dir)) return [];
	return readdirSync(dir)
		.filter((f) => f.endsWith('.svelte'))
		.map((f) => ({
			file: resolve(dir, f),
			key: `${zone}/${f}`,
			src: readFileSync(resolve(dir, f), 'utf8'),
		}));
}

const PANELS = zoneDirs().flatMap(panelsOf);

describe('a dock panel is not a mirror of its page', () => {
	it('there are panels to check at all', () => {
		expect(PANELS.length).toBeGreaterThan(0);
	});

	it.each(PANELS)('$key renders a shared component', ({ key, src }) => {
		if (key in NO_PAGE_COUNTERPART) return;
		// A component import from the zone's own lib — `$lib/…/Thing.svelte`. The dock's own plumbing
		// ($lib/dock/*) does not count: every panel imports that.
		const shared = [...src.matchAll(/from\s+'(\$lib\/[^']+\.svelte)'/g)]
			.map((m) => m[1]!)
			.filter((p) => !p.startsWith('$lib/dock/'));
		expect(
			shared.length,
			`${key} imports no shared component. A panel must render the SAME component its route does ` +
				`(see $lib/boards/*Board.svelte), not re-draw the view over the same remote — that is how ` +
				`the panel and the page drift. If this panel genuinely has no page counterpart, add it to ` +
				`NO_PAGE_COUNTERPART with the reason.`,
		).toBeGreaterThan(0);
	});
});
