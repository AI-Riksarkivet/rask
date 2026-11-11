import { describe, expect, it } from 'vitest';
import type { LayoutConfig } from 'horizon-layout';
import { defaultLayout } from './layout.ts';
import { openWorkbenchStore, viewsOf } from './store.ts';
import { DockViews } from './views.svelte.ts';

/** A `Storage` over a Map: the test environment has no DOM. */
function memoryStorage(initial: Record<string, string> = {}): Storage {
	const kept = new Map(Object.entries(initial));
	return {
		getItem: (key) => kept.get(key) ?? null,
		setItem: (key, value) => void kept.set(key, value),
		removeItem: (key) => void kept.delete(key),
		clear: () => kept.clear(),
		key: (index) => [...kept.keys()][index] ?? null,
		get length() {
			return kept.size;
		},
	};
}

const tablesOnly: LayoutConfig = { root: { tabs: ['tables'], activeTabIndex: 0 } };

/** The sidebar's state model over a store, with the dock's live layout in `live.layout`. */
async function viewsOver(storage: Storage) {
	const store = await openWorkbenchStore(null, () => storage);
	const live: { layout: LayoutConfig } = { layout: defaultLayout() };
	const views = new DockViews(viewsOf(store), () => live.layout);
	await views.refresh();
	return { views, live };
}

describe('the named views', () => {
	it('refuses every write while the stored views cannot be read', async () => {
		const stored = JSON.stringify({ views: 'not a list' });
		const storage = memoryStorage({ 'rask-workbench:local': stored });
		const { views } = await viewsOver(storage);
		expect(views.phase).toBe('unreadable');

		expect(await views.saveAs('X')).toBe(false);
		expect(await views.rename('a', 'Y')).toBe(false);
		expect(await views.remove('a')).toBe(false);
		expect(views.lastError).toMatch(/refused/);
		expect(storage.getItem('rask-workbench:local')).toBe(stored);
	});

	it('marks the active view modified when the dock moves away from it, and clears it on the way back', async () => {
		const { views, live } = await viewsOver(memoryStorage());
		expect(await views.saveAs('Both')).toBe(true);
		expect(views.dirty).toBe(false);

		live.layout = tablesOnly;
		views.touch();
		expect(views.dirty).toBe(true);

		live.layout = defaultLayout();
		views.touch();
		expect(views.dirty).toBe(false);
	});

	it('takes the saved arrangement as the new baseline', async () => {
		const { views, live } = await viewsOver(memoryStorage());
		await views.saveAs('Both');
		live.layout = tablesOnly;
		views.touch();
		expect(await views.save()).toBe(true);
		expect(views.dirty).toBe(false);
		expect(views.select(views.activeId ?? '')).toEqual({ status: 'ok', layout: tablesOnly });
	});

	it('deleting the active view leaves the arrangement on screen as the draft', async () => {
		const { views, live } = await viewsOver(memoryStorage());
		await views.saveAs('Both');
		const id = views.activeId ?? '';
		expect(await views.remove(id)).toBe(true);
		expect(views.activeId).toBeNull();
		expect(views.views).toEqual([]);
		expect(live.layout).toEqual(defaultLayout());
	});
});
