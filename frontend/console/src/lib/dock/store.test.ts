import { afterEach, describe, expect, it, vi } from 'vitest';
import { LayoutAutosave } from './autosave.ts';
import { defaultLayout } from './layout.ts';
import { openWorkbenchStore, viewsOf } from './store.ts';
import type { LayoutConfig } from 'horizon-layout';

/** A `Storage` over a Map: the test environment has no DOM, as `activity.test.ts` does it. */
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

const refusing = (): Storage => {
	throw new DOMException('denied', 'SecurityError');
};

afterEach(() => {
	vi.useRealTimers();
});

describe('the workbench store', () => {
	it('keys a person by a hash of their id, and gives back the layout and views they stored', async () => {
		const storage = memoryStorage();
		const store = await openWorkbenchStore('user:alice', () => storage);
		expect(store.loadLayout()).toEqual({ status: 'absent' });

		const layout: LayoutConfig = { root: { tabs: ['events', 'tables'], activeTabIndex: 1 } };
		store.saveLayout(layout);
		await viewsOf(store).create('Both', layout);

		const key = storage.key(0) ?? '';
		expect(key).toMatch(/^rask-workbench:[0-9a-f]{64}$/);
		expect(key).not.toContain('alice');
		const again = await openWorkbenchStore('user:alice', () => storage);
		expect(again.loadLayout()).toEqual({ status: 'ok', layout });
		expect(again.loadViews()).toMatchObject({ status: 'ok', views: [{ name: 'Both', layout }] });
		expect((await openWorkbenchStore('user:bob', () => storage)).loadLayout().status).toBe(
			'absent',
		);
	});

	it('calls a stored layout it cannot show unreadable, saves nothing over it until a reset', async () => {
		vi.useFakeTimers();
		const stored = JSON.stringify({ layout: { root: { tabs: ['gone'], activeTabIndex: 0 } } });
		const storage = memoryStorage({ 'rask-workbench:local': stored });

		const store = await openWorkbenchStore(null, () => storage);
		const autosave = new LayoutAutosave(store, 10);
		expect(autosave.restore().status).toBe('unreadable');
		autosave.schedule(defaultLayout());
		await vi.advanceTimersByTimeAsync(50);
		expect(storage.getItem('rask-workbench:local')).toBe(stored);

		autosave.reset(defaultLayout());
		expect(JSON.parse(storage.getItem('rask-workbench:local') ?? '')).toEqual({
			layout: defaultLayout(),
		});
	});

	it('works from memory, and says so, where the browser refuses storage', async () => {
		const store = await openWorkbenchStore('user:alice', refusing);
		expect(store.persistent).toBe(false);
		expect(store.loadLayout()).toEqual({ status: 'absent' });
		store.saveLayout(defaultLayout());
		expect(store.loadLayout()).toEqual({ status: 'ok', layout: defaultLayout() });
	});

	it('lets two tabs of one person write without either erasing what the other saved', async () => {
		const storage = memoryStorage();
		const tabA = await openWorkbenchStore('user:alice', () => storage);
		const tabB = await openWorkbenchStore('user:alice', () => storage);

		expect(await viewsOf(tabB).create('Kept', defaultLayout())).not.toBeNull();
		tabA.saveLayout({ root: { tabs: ['tables'], activeTabIndex: 0 } });

		const reloaded = await openWorkbenchStore('user:alice', () => storage);
		expect(reloaded.loadViews()).toMatchObject({ status: 'ok', views: [{ name: 'Kept' }] });
		expect(reloaded.loadLayout()).toEqual({
			status: 'ok',
			layout: { root: { tabs: ['tables'], activeTabIndex: 0 } },
		});
	});

	it('refuses a write over a document another tab has since made unreadable', async () => {
		const storage = memoryStorage({
			'rask-workbench:local': JSON.stringify({ layout: defaultLayout() }),
		});
		const store = await openWorkbenchStore(null, () => storage);
		expect(store.loadLayout().status).toBe('ok');

		storage.setItem('rask-workbench:local', '{not json');
		expect(store.saveLayout(defaultLayout())).toBe(false);
		expect(await viewsOf(store).create('X', defaultLayout())).toBeNull();
		expect(storage.getItem('rask-workbench:local')).toBe('{not json');
	});

	it('keeps unreadable views, and the layout beside them, until the views are reset', async () => {
		const stored = JSON.stringify({ layout: defaultLayout(), views: [{ id: 1 }] });
		const storage = memoryStorage({ 'rask-workbench:local': stored });
		const store = await openWorkbenchStore(null, () => storage);
		const views = viewsOf(store);
		expect((await views.list()).status).toBe('unreadable');
		expect(await views.create('X', defaultLayout())).toBeNull();
		expect(storage.getItem('rask-workbench:local')).toBe(stored);

		expect(await views.reset()).toBe(true);
		expect(await views.list()).toEqual({ status: 'ok', views: [] });
		expect(store.loadLayout()).toEqual({ status: 'ok', layout: defaultLayout() });
	});

	it('stops saving, and says why, when a save finds the stored document unreadable', async () => {
		vi.useFakeTimers();
		const storage = memoryStorage({
			'rask-workbench:local': JSON.stringify({ layout: defaultLayout() }),
		});
		const store = await openWorkbenchStore(null, () => storage);
		const refused: string[] = [];
		const autosave = new LayoutAutosave(store, 10, (detail) => refused.push(detail));
		expect(autosave.restore().status).toBe('ok');

		storage.setItem('rask-workbench:local', '{not json');
		autosave.schedule({ root: { tabs: ['tables'], activeTabIndex: 0 } });
		await vi.advanceTimersByTimeAsync(50);
		expect(autosave.saving).toBe(false);
		expect(refused).toHaveLength(1);
		expect(storage.getItem('rask-workbench:local')).toBe('{not json');
	});
});
