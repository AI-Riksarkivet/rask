import { describe, expect, it, vi } from 'vitest';
import { defaultLayout } from './layout.ts';
import type { LayoutRead } from './layout.ts';
import { startWorkbench, type WorkbenchSession } from './session.ts';
import { openWorkbenchStore, type WorkbenchStore } from './store.ts';

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

/** Start a workbench as a page does, and answer the session it was handed: null when none started. */
async function start(
	opening: Promise<WorkbenchStore>,
	left: () => boolean = () => false,
	onLayout: (read: LayoutRead) => void = () => {},
): Promise<WorkbenchSession | null> {
	const held: WorkbenchSession[] = [];
	await startWorkbench(
		opening,
		{ snapshot: () => defaultLayout(), onLayout, started: (session) => held.push(session) },
		left,
	);
	return held[0] ?? null;
}

describe('starting the workbench', () => {
	it('starts nothing and listens to nothing when the page left before its store opened', async () => {
		const store = await openWorkbenchStore(null, () => memoryStorage());
		const listen = vi.spyOn(store, 'onChange');
		let open: (store: WorkbenchStore) => void = () => {};
		const opening = new Promise<WorkbenchStore>((resolve) => (open = resolve));
		let left = false;

		const started = start(opening, () => left);
		left = true;
		open(store);

		expect(await started).toBeNull();
		expect(listen).not.toHaveBeenCalled();
	});

	it('hands the page its session in the turn its listeners are added', async () => {
		const store = await openWorkbenchStore(null, () => memoryStorage());
		const order: string[] = [];
		const listen = store.onChange.bind(store);
		vi.spyOn(store, 'onChange').mockImplementation((listener) => {
			queueMicrotask(() => order.push('a microtask later'));
			order.push('listening');
			return listen(listener);
		});

		await startWorkbench(
			Promise.resolve(store),
			{
				snapshot: () => defaultLayout(),
				onLayout: () => {},
				started: (session) => {
					order.push('held');
					session.stop();
				},
			},
			() => false,
		);
		await Promise.resolve();

		expect(order).toEqual(['listening', 'held', 'a microtask later']);
	});

	it('tells the page the stored layout is readable again once a views reset replaced it', async () => {
		const storage = memoryStorage({ 'rask-workbench:local': '{not json' });
		const reads: string[] = [];
		const session = await start(
			openWorkbenchStore(null, () => storage),
			() => false,
			(read) => reads.push(read.status),
		);
		expect(session?.restored.status).toBe('unreadable');

		expect(await session?.views.reset()).toBe(true);
		expect(reads.at(-1)).toBe('absent');
		expect(session?.autosave.saving).toBe(true);
		session?.stop();
	});
});
