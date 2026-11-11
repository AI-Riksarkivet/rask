import { describe, expect, it } from 'vitest';
import { validateConfig, type Id } from 'horizon-layout';
import {
	PANELS,
	closePanel,
	closedPanels,
	defaultLayout,
	openPanel,
	popOut,
	readStoredLayout,
} from './layout.ts';

const registry: ReadonlyMap<Id, true> = new Map(PANELS.map((p) => [p.id, true] as const));

describe('the panel registry and the default layout', () => {
	it('names each panel once, and the first visit opens every panel side by side', () => {
		expect(new Set(PANELS.map((p) => p.id)).size).toBe(PANELS.length);
		const layout = defaultLayout();
		expect(() => validateConfig(layout, registry)).not.toThrow();
		expect(closedPanels(layout)).toEqual([]);
	});
});

describe('closing and opening a panel', () => {
	it('closes a panel out of its split and opens it again beside the panel whose control was used', () => {
		const closed = closePanel(defaultLayout(), 'events');
		expect(closed.root).toEqual({ tabs: ['tables'], activeTabIndex: 0 });
		expect(closedPanels(closed)).toEqual(['events']);

		const reopened = openPanel(closed, 'events', 'tables');
		expect(reopened.root).toEqual({ tabs: ['tables', 'events'], activeTabIndex: 1 });
		expect(() => validateConfig(reopened, registry)).not.toThrow();
	});

	it('counts a popped-out panel as open, and closing it drops the window', () => {
		const out = popOut(defaultLayout(), 'events');
		expect(out.popouts).toEqual(['events']);
		expect(closedPanels(out)).toEqual([]);
		expect(closePanel(out, 'events').popouts).toBeUndefined();
	});
});

describe('reading a stored layout', () => {
	it('accepts a layout of known panels and calls anything else unreadable', () => {
		expect(readStoredLayout(JSON.parse(JSON.stringify(defaultLayout())))).toEqual({
			status: 'ok',
			layout: defaultLayout(),
		});
		expect(readStoredLayout({ root: { tabs: ['gone'], activeTabIndex: 0 } }).status).toBe(
			'unreadable',
		);
		expect(readStoredLayout({ root: { tabs: [] } }).status).toBe('unreadable');
		expect(readStoredLayout('not a layout').status).toBe('unreadable');
	});

	it('docks the panels a stored layout had popped out, since no window survives a reload', () => {
		expect(readStoredLayout({ popouts: ['events', 'tables'] })).toEqual({
			status: 'ok',
			layout: { root: { tabs: ['events', 'tables'], activeTabIndex: 1 } },
		});
	});
});
