import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import type { DockviewApi } from 'dockview';
import { uniquePanelId } from './panel-id';
import { matchesPanel, panelChoices, type PanelChoice } from './panel-search';
import type { PanelRegistry } from './types';

/**
 * The `+` picker's two pure halves: which id a new panel gets, and which rows the search shows.
 *
 * Both are deliberately free of dockview and of the DOM, so they can be tested at all — the picker
 * COMPONENT cannot be, in a repo with no DOM environment, and the browser half is a Playwright proof.
 */

/** A registry that exercises label/key/keyword matching separately. */
const REGISTRY = {
	graph: { component: (() => {}) as never, label: 'Graph', keywords: ['dag', 'provenance'] },
	runs: { component: (() => {}) as never, label: 'Runs', keywords: ['jobs', 'executions'] },
	topics: { component: (() => {}) as never, label: 'Topic results' },
} as unknown as PanelRegistry;

const choice = (over: Partial<PanelChoice> = {}): PanelChoice =>
	({ key: 'graph', label: 'Graph', keywords: ['dag'], open: false, ...over }) as PanelChoice;

/** A container api whose `getPanel` reports the given ids as taken. */
const apiOf = (taken: string[]) =>
	({ getPanel: (id: string) => (taken.includes(id) ? {} : undefined) }) as unknown as DockviewApi;

describe('uniquePanelId', () => {
	it('returns the base when nothing holds it', () => {
		expect(uniquePanelId(apiOf([]), 'runs')).toBe('runs');
	});

	it('probes upward past every taken id', () => {
		// `addPanel` THROWS on a duplicate, so this is the difference between a working button and an
		// uncaught exception inside a click handler.
		expect(uniquePanelId(apiOf(['runs']), 'runs')).toBe('runs-2');
		expect(uniquePanelId(apiOf(['runs', 'runs-2', 'runs-3']), 'runs')).toBe('runs-4');
	});

	it('reads a persisted layout the way a human would — no random suffixes', () => {
		expect(uniquePanelId(apiOf(['runs']), 'runs')).toMatch(/^runs-\d+$/);
	});
});

describe('matchesPanel', () => {
	it('shows everything for an empty or whitespace query', () => {
		expect(matchesPanel(choice(), '')).toBe(true);
		expect(matchesPanel(choice(), '   ')).toBe(true);
	});

	it('matches the label, case-insensitively and mid-word', () => {
		expect(matchesPanel(choice({ label: 'Topic results' }), 'TOPIC')).toBe(true);
		// Mid-word matters for a nine-item list: "pic" should find "Topic results".
		expect(matchesPanel(choice({ label: 'Topic results' }), 'pic')).toBe(true);
	});

	it('matches the registry key and the keywords, not just the label', () => {
		// The whole reason keywords exist: nobody looking for lineage provenance types "Graph".
		expect(matchesPanel(choice({ label: 'Graph', keywords: ['provenance'] }), 'provenance')).toBe(
			true,
		);
		expect(matchesPanel(choice({ key: 'treemap', label: 'Topics' }), 'treemap')).toBe(true);
	});

	it('ANDs whitespace-separated terms across fields', () => {
		// The one thing plain `includes()` gets wrong often enough to be worth four extra lines: the
		// terms need not be adjacent, or even in the same field.
		expect(matchesPanel(choice({ label: 'Topic results' }), 'topic res')).toBe(true);
		expect(matchesPanel(choice({ label: 'Topic results', keywords: ['hits'] }), 'topic hits')).toBe(
			true,
		);
		expect(matchesPanel(choice({ label: 'Topic results' }), 'topic missing')).toBe(false);
	});

	it('does not match an unrelated query', () => {
		expect(matchesPanel(choice({ label: 'Graph', keywords: ['dag'] }), 'zzz')).toBe(false);
	});
});

describe('panelChoices', () => {
	it('keeps registry order and carries the key alongside each entry', () => {
		expect(panelChoices(REGISTRY, []).map((c) => c.key)).toEqual(['graph', 'runs', 'topics']);
	});

	it('marks which components are already open WITHOUT removing them', () => {
		// Listed, not hidden and not disabled: two Runs panels filtered differently is a real thing to
		// want, and `uniquePanelId` exists precisely so the second copy is safe. Hiding them would also
		// make the list jump around as the user works.
		const open = panelChoices(REGISTRY, ['runs']);
		expect(open).toHaveLength(3);
		expect(open.find((c) => c.key === 'runs')?.open).toBe(true);
		expect(open.find((c) => c.key === 'graph')?.open).toBe(false);
	});
});

describe('the header offers "+" as a control separate from split', () => {
	it('renders both triggers, and they open different popovers', () => {
		// Fails on the previous code, which had no add control at all. The separation is the point:
		// split acts on the PANE, "+" acts on the CONTENT. An earlier draft of the backlog had add-panel
		// REPLACING split, which is what produced a control whose only outcome was a second copy of
		// whatever you were already looking at.
		const header = readFileSync(new URL('./GroupActions.svelte', import.meta.url), 'utf8');
		expect(header).toContain('PanelPicker');
		expect(header).toContain('SplitMenu');
		expect(header).toContain('rask-add-menu-');
		expect(header).toContain('rask-split-menu-');
	});

	it('gives every group its own popover ids, so one trigger cannot open another group’s menu', () => {
		// Derived from `group.id`, NOT Svelte's `$props.id()`: each group header is its own mount() root
		// and that counter restarts per root, so every group would mint the same id.
		const header = readFileSync(new URL('./GroupActions.svelte', import.meta.url), 'utf8');
		expect(header).toMatch(/rask-add-menu-\$\{group\.id\}/);
		expect(header).toMatch(/rask-split-menu-\$\{group\.id\}/);
	});
});
