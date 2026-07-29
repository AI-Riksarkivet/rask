import { describe, expect, it, vi } from 'vitest';
import type { ContextMenuItem, GetTabContextMenuItemsParams } from 'dockview';
import { DEFAULT_CHROME, resolveChrome, type DockChrome, type DockChromeOptions } from './chrome';
import { makeTabContextMenu } from './context-menu';
import { SPLIT_DIRECTIONS } from './split';

/**
 * The tab context menu — the long tail of dock actions, and until now untested.
 *
 * It is a pure function of (chrome, options, params) returning an array, which makes it one of the
 * few genuinely DOM-free surfaces in this package. Worth pinning because it is also the ONLY place
 * several actions exist at all: with the header offering one split trigger, up/left used to live here
 * exclusively, and `chrome.split === false` silently removing four rows is invisible from any zone.
 */

/** The labels of the custom rows, ignoring built-in strings and separators. */
const labels = (items: ContextMenuItem[]): string[] =>
	items
		.filter((i): i is { label: string; action: () => void } => typeof i !== 'string')
		.map((i) => i.label);

const paramsOf = ({
	panels = 2,
	location = 'grid' as unknown,
	maximized = false,
} = {}): GetTabContextMenuItemsParams & { api: { addFloatingGroup: ReturnType<typeof vi.fn> } } =>
	({
		panel: { id: 'runs', api: { component: 'runs', moveTo: vi.fn() } },
		group: {
			panels: Array.from({ length: panels }, (_, i) => ({ id: `p${i}` })),
			api: {
				location,
				isMaximized: () => maximized,
				maximize: vi.fn(),
				exitMaximized: vi.fn(),
				moveTo: vi.fn(),
			},
		},
		api: {
			addFloatingGroup: vi.fn(),
			addPopoutGroup: vi.fn(() => Promise.resolve(true)),
			addPanel: vi.fn(),
			getPanel: () => undefined,
		},
	}) as unknown as GetTabContextMenuItemsParams & {
		api: { addFloatingGroup: ReturnType<typeof vi.fn> };
	};

const build = (
	chrome: DockChrome = DEFAULT_CHROME,
	options: DockChromeOptions = { popoutUrl: '/popout.html' },
) => makeTabContextMenu(chrome, options)(paramsOf());

describe('makeTabContextMenu', () => {
	it('always offers dockview’s three built-in close rows', () => {
		// Only FOUR built-in strings exist in 7.0.4 — close, closeOthers, closeAll, separator. The
		// upstream docs also list closeLeft/closeRight/maximize; those are master-branch drift.
		expect(build().slice(0, 3)).toEqual(['close', 'closeOthers', 'closeAll']);
	});

	it('offers every one of the four split directions, in SPLIT_DIRECTIONS order', () => {
		// Same table the header's compass pad renders from. Before the table existed this file spelled
		// the four out itself, in right/left/down/up order, so the two surfaces could disagree on both
		// membership and order with nothing to compare them against.
		expect(labels(build()).slice(0, 4)).toEqual(SPLIT_DIRECTIONS.map((d) => `Split ${d.word}`));
	});

	it('says Duplicate, not Split, when the group holds one panel', () => {
		// The label must match what the click does — with one panel `moveTo` is a documented no-op, so
		// splitPanel duplicates instead.
		const items = makeTabContextMenu(DEFAULT_CHROME, {})(paramsOf({ panels: 1 }));
		expect(
			labels(items)
				.slice(0, 4)
				.every((l) => l.startsWith('Duplicate')),
		).toBe(true);
	});

	it('drops the split rows entirely when chrome.split is off', () => {
		const items = makeTabContextMenu(resolveChrome({ split: false }), {})(paramsOf());
		expect(labels(items).some((l) => /^(Split|Duplicate)/.test(l))).toBe(false);
	});

	it('offers popout only when the zone actually configured a popout url', () => {
		// The header button self-disables and explains itself; a context-menu row has no such surface,
		// so a row that silently does nothing is the worse failure and must simply not be there.
		expect(labels(build(DEFAULT_CHROME, {})).some((l) => l === 'Open in a new window')).toBe(false);
		expect(labels(build()).some((l) => l === 'Open in a new window')).toBe(true);
	});

	it('flips maximize to Restore while the group is maximized', () => {
		const items = makeTabContextMenu(DEFAULT_CHROME, {})(paramsOf({ maximized: true }));
		expect(labels(items)).toContain('Restore');
		expect(labels(items)).not.toContain('Maximize this group');
	});

	it('offers no split or maximize rows for a floating group — there is no grid to act in', () => {
		const items = makeTabContextMenu(
			DEFAULT_CHROME,
			{},
		)(paramsOf({ location: { type: 'floating' } }));
		expect(labels(items).some((l) => /^(Split|Duplicate)/.test(l))).toBe(false);
		expect(labels(items)).toContain('Dock back into the layout');
	});

	it('a bare dock (chrome false) offers nothing but the built-in close rows', () => {
		const items = makeTabContextMenu(resolveChrome(false), { popoutUrl: '/p.html' })(paramsOf());
		expect(labels(items)).toEqual([]);
		expect(items).toEqual(['close', 'closeOthers', 'closeAll']);
	});
});
