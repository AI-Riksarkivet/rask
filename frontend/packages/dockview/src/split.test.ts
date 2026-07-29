import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import type { DockviewApi, DockviewGroupPanel, IDockviewPanel } from 'dockview';
import { SPLIT_DIRECTIONS, splitPanel, splitVerb } from './split';

/**
 * WHAT SPLIT ACTUALLY DOES — pinned, because the backlog was wrong about it.
 *
 * `open_dockview.md` claimed split "only ever duplicates the ACTIVE panel … so the sole thing a user
 * can create is a second copy of what they are already looking at". That is false, and building the
 * G1 direction affordance on top of it would have meant rewriting a correct function. The truth is
 * the two-mode behaviour `split.ts` documents: MOVE at 2+ panels, DUPLICATE at exactly 1 — because
 * dockview's `moveTo` onto a panel's own single-panel group is a silent no-op, so duplicating is the
 * only thing that can visibly happen there.
 *
 * These tests exist so the next person to read that sentence finds a failing assertion instead of a
 * plausible paragraph. The file it described is unchanged.
 *
 * Collaborators arrive as arguments, so this needs no DOM — see `vitest.config.ts` on why that is a
 * hard constraint here and where the browser-level proofs live instead.
 */

/** A group holding `n` panels. Only `panels.length` is read by the code under test. */
const groupOf = (n: number) =>
	({
		panels: Array.from({ length: n }, (_, i) => ({ id: `panel-${i}` })),
	}) as unknown as DockviewGroupPanel;

const panelOf = (
	id: string,
	{ component = 'runs', title = 'Runs', params = { a: 1 } } = {},
): IDockviewPanel & { api: { moveTo: ReturnType<typeof vi.fn> } } =>
	({
		id,
		title,
		params,
		api: { component, moveTo: vi.fn() },
	}) as unknown as IDockviewPanel & { api: { moveTo: ReturnType<typeof vi.fn> } };

/** A container api whose `getPanel` reports the given ids as taken. */
const apiOf = (taken: string[] = []) =>
	({
		getPanel: (id: string) => (taken.includes(id) ? {} : undefined),
		addPanel: vi.fn(),
	}) as unknown as DockviewApi & { addPanel: ReturnType<typeof vi.fn> };

describe('splitPanel', () => {
	it('MOVES the active panel when the group holds more than one — it does not duplicate', () => {
		// The assertion the backlog doc would have failed. A move is the real split: the panel leaves
		// its group for a new adjacent one, and no second copy is created.
		const group = groupOf(3);
		const panel = panelOf('runs');
		const api = apiOf();

		splitPanel(api, group, panel, 'right');

		expect(panel.api.moveTo).toHaveBeenCalledWith({ group, position: 'right' });
		expect(api.addPanel).not.toHaveBeenCalled();
	});

	it('DUPLICATES only when the group holds exactly one panel', () => {
		// `moveTo` onto your own single-panel group takes dockview's `sourceGroup.size < 2` branch and
		// resolves to `moveView(parent, from, to)` with from === to. Nothing happens, nothing throws —
		// the button would just appear broken, which is why this branch exists at all.
		const group = groupOf(1);
		const panel = panelOf('runs');
		const api = apiOf();

		splitPanel(api, group, panel, 'right');

		expect(panel.api.moveTo).not.toHaveBeenCalled();
		expect(api.addPanel).toHaveBeenCalledTimes(1);
	});

	it.each([
		['left', 'left'],
		['right', 'right'],
		['top', 'above'],
		['bottom', 'below'],
	] as const)('maps the %s drop anchor to the %s grid direction', (position, direction) => {
		// Two vocabularies for the same four sides: `position` is a drop anchor, `direction` is a grid
		// placement, and top/bottom are spelled above/below on the grid. Getting one wrong puts the pane
		// silently on the wrong side — visible to a user, invisible to a type checker.
		const api = apiOf();
		splitPanel(api, groupOf(1), panelOf('runs'), position);

		expect(api.addPanel.mock.calls[0]?.[0]?.position).toEqual({
			referenceGroup: expect.anything(),
			direction,
		});
	});

	it('carries the source panel’s component, title and params into the duplicate', () => {
		// `component` is the string a serialized layout resolves by; dropping it would produce a panel
		// that restores as MissingPanelRenderer on the next load.
		const api = apiOf();
		splitPanel(api, groupOf(1), panelOf('runs', { component: 'graph', title: 'Graph' }), 'right');

		expect(api.addPanel.mock.calls[0]?.[0]).toMatchObject({
			component: 'graph',
			title: 'Graph',
			params: { a: 1 },
		});
	});

	it('probes upward for a free id rather than colliding or appending randomness', () => {
		// Ids are unique across the dock and a persisted layout should stay readable to a human, so the
		// third copy of `runs` is `runs-4` — not a uuid, and never a second `runs-2`.
		const api = apiOf(['runs-2', 'runs-3']);
		splitPanel(api, groupOf(1), panelOf('runs'), 'right');

		expect(api.addPanel.mock.calls[0]?.[0]?.id).toBe('runs-4');
	});

	it('never reuses the source panel’s own id', () => {
		const api = apiOf(['runs']);
		splitPanel(api, groupOf(1), panelOf('runs'), 'bottom');

		expect(api.addPanel.mock.calls[0]?.[0]?.id).not.toBe('runs');
	});
});

describe('SPLIT_DIRECTIONS', () => {
	it('covers all four sides exactly once', () => {
		expect(SPLIT_DIRECTIONS.map((d) => d.position).sort()).toEqual([
			'bottom',
			'left',
			'right',
			'top',
		]);
	});

	it('every direction actually reaches splitPanel and lands on a distinct grid side', () => {
		// The point of the table is that all four are WIRED, not merely listed. Before this change the
		// header called split() with two literals and the context menu had its own copy of the list, so
		// "are all four reachable?" had no single answer to assert.
		const seen = SPLIT_DIRECTIONS.map((d) => {
			const api = apiOf();
			splitPanel(api, groupOf(1), panelOf('runs'), d.position);
			return api.addPanel.mock.calls[0]?.[0]?.position?.direction as string;
		});
		expect(new Set(seen).size).toBe(4);
		expect(seen.sort()).toEqual(['above', 'below', 'left', 'right']);
	});

	it('the HEADER offers all four — it no longer hardcodes two', () => {
		// The assertion that fails on the previous code. GroupActions used to render two buttons wired
		// to split('right') and split('bottom'); up and left existed only as tab-context-menu rows,
		// which is an interaction nobody discovers. Source containment because the header is a component
		// and this repo has no DOM environment to mount one in — the real click-through is a Playwright
		// proof.
		const header = readFileSync(new URL('./GroupActions.svelte', import.meta.url), 'utf8');
		expect(header, 'the header does not mount the direction pad').toContain('SplitMenu');
		expect(header, 'the header still hardcodes a single split direction').not.toMatch(
			/split\('(right|bottom|left|top)'\)/,
		);
	});
});

describe('splitVerb', () => {
	it('says Split when the control will move, and Duplicate when it will copy', () => {
		// The label must match what the click does; a control that silently does something other than
		// its title is worse than one that is honest about a narrower behaviour.
		expect(splitVerb(2)).toBe('Split');
		expect(splitVerb(1)).toBe('Duplicate');
		expect(splitVerb(0)).toBe('Duplicate');
	});

	it('is the rule BOTH surfaces use — the header no longer keeps its own copy', () => {
		// Guards the drift this signature change fixed: `GroupActions.svelte` derived the verb from an
		// inline ternary while `context-menu.ts` called `splitVerb`, so the two could disagree and only
		// a browser would show it. Asserted as source containment because the header is a component and
		// this repo has no DOM test environment to mount one in.
		const header = readFileSync(new URL('./GroupActions.svelte', import.meta.url), 'utf8');
		expect(header).toContain('splitVerb');
		expect(header, 'the header re-implemented the verb rule instead of importing it').not.toMatch(
			/\?\s*'Split'\s*:\s*'Duplicate'/,
		);
	});
});
