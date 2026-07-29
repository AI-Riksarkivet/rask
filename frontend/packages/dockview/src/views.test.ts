import { describe, expect, it } from 'vitest';
import type { DockView, DockViewsStore } from '@rask/api/dock-views';
import { DockViews } from './views.svelte';

/**
 * The saved-views state model, and above all the decision the backlog skipped:
 *
 *   Once a view is loaded and the user drags a sash, autosave keeps writing the implicit DRAFT and the
 *   named view stays frozen until an explicit Save.
 *
 * Both alternatives are silent and worse — overwriting means you cannot open a saved arrangement
 * without destroying it; forking fills the sidebar with layouts nobody asked for. So the gap is SHOWN
 * (`dirty`) rather than resolved behind the user's back, and these tests pin that it is shown
 * accurately: it appears on divergence, and it CLEARS itself if the user drags back.
 */

type L = { g: number };

/** An in-memory library with the same three-outcome read contract as the real transport. */
function fakeStore(
	initial: DockView<L>[] = [],
	opts: { unreadable?: boolean; refuse?: boolean } = {},
) {
	let views = [...initial];
	let n = 0;
	const store: DockViewsStore<L> = {
		list: async () =>
			opts.unreadable ? { status: 'unreadable', detail: 'boom' } : { status: 'ok', layout: views },
		create: async (name, layout) => {
			if (opts.refuse) return null;
			const v: DockView<L> = { id: `v${++n}`, name, layout, updated: 'T' };
			views = [v, ...views];
			return v;
		},
		update: async (id, layout) => {
			if (opts.refuse) return false;
			views = views.map((v) => (v.id === id ? { ...v, layout } : v));
			return true;
		},
		rename: async (id, name) => {
			if (opts.refuse) return false;
			views = views.map((v) => (v.id === id ? { ...v, name } : v));
			return true;
		},
		remove: async (id) => {
			if (opts.refuse) return false;
			views = views.filter((v) => v.id !== id);
			return true;
		},
	};
	return { store, peek: () => views };
}

const view = (id: string, name: string, g: number): DockView<L> => ({
	id,
	name,
	layout: { g },
	updated: 'T',
});

describe('the divergence marker — the decision that was missing', () => {
	it('is false with no view active: the draft is not dirty against nothing', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)]);
		const dock = new DockViews(store, () => ({ g: 99 }));
		await dock.refresh();
		expect(dock.dirty).toBe(false);
	});

	it('is false immediately after loading a view', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)]);
		let live: L = { g: 0 };
		const dock = new DockViews(store, () => live);
		await dock.refresh();
		live = { g: 1 };
		dock.select('v1');
		expect(dock.dirty).toBe(false);
	});

	it('becomes true when the arrangement moves away from the loaded view', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)]);
		let live: L = { g: 1 };
		const dock = new DockViews(store, () => live);
		await dock.refresh();
		dock.select('v1');

		live = { g: 2 };
		dock.touch();
		expect(dock.dirty).toBe(true);
	});

	it('CLEARS ITSELF when the user drags back — a flag could not do this', async () => {
		// The reason `dirty` is a comparison and not a boolean set on every mutation. A flag stays true
		// forever once tripped, and would show a modified marker on an arrangement that matches its view
		// exactly.
		const { store } = fakeStore([view('v1', 'A', 1)]);
		let live: L = { g: 1 };
		const dock = new DockViews(store, () => live);
		await dock.refresh();
		dock.select('v1');

		live = { g: 2 };
		dock.touch();
		expect(dock.dirty).toBe(true);

		live = { g: 1 };
		dock.touch();
		expect(dock.dirty).toBe(false);
	});

	it('an explicit save re-baselines, so the marker clears', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)]);
		let live: L = { g: 1 };
		const dock = new DockViews(store, () => live);
		await dock.refresh();
		dock.select('v1');
		live = { g: 5 };
		dock.touch();

		expect(await dock.save()).toBe(true);
		expect(dock.dirty).toBe(false);
	});
});

describe('the list, the active view, and what the UI reads', () => {
	it('exposes summaries without the layout payload', async () => {
		const { store } = fakeStore([view('v1', 'A', 1), view('v2', 'B', 2)]);
		const dock = new DockViews(store, () => ({ g: 0 }));
		await dock.refresh();
		expect(dock.views).toEqual([
			{ id: 'v1', name: 'A', updated: 'T' },
			{ id: 'v2', name: 'B', updated: 'T' },
		]);
	});

	it('select returns the dock’s own three-outcome contract, absent for a stale click', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)]);
		const dock = new DockViews(store, () => ({ g: 0 }));
		await dock.refresh();

		expect(dock.select('v1')).toEqual({ status: 'ok', layout: { g: 1 } });
		// A stale sidebar click is ordinary, not a crash.
		expect(dock.select('gone')).toEqual({ status: 'absent' });
	});

	it('saveAs creates, activates, and puts the new view first', async () => {
		const { store } = fakeStore();
		const dock = new DockViews(store, () => ({ g: 7 }));
		await dock.refresh();

		expect(await dock.saveAs('Compare two runs')).toBe(true);
		expect(dock.views[0]?.name).toBe('Compare two runs');
		expect(dock.active?.name).toBe('Compare two runs');
		expect(dock.dirty).toBe(false);
	});

	it('refuses a blank name rather than creating an unnameable row', async () => {
		const { store } = fakeStore();
		const dock = new DockViews(store, () => ({ g: 1 }));
		await dock.refresh();
		expect(await dock.saveAs('   ')).toBe(false);
		expect(dock.views).toHaveLength(0);
	});

	it('deleting the active view leaves the arrangement alone and drops the selection', async () => {
		// Clearing the dock would destroy work the user never asked to lose — they deleted a bookmark,
		// not the thing it pointed at.
		const { store } = fakeStore([view('v1', 'A', 1)]);
		const dock = new DockViews(store, () => ({ g: 1 }));
		await dock.refresh();
		dock.select('v1');

		expect(await dock.remove('v1')).toBe(true);
		expect(dock.activeId).toBeNull();
		expect(dock.dirty).toBe(false);
	});

	it('a refresh that loses the active view clears the selection', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)]);
		const dock = new DockViews(store, () => ({ g: 1 }));
		await dock.refresh();
		dock.select('v1');

		await store.remove('v1');
		await dock.refresh();
		expect(dock.activeId).toBeNull();
	});
});

describe('unreadable still refuses to write — through the NAMED path', () => {
	it('reports unreadable rather than an empty library', async () => {
		// Reporting an unreadable library as "no saved views" invites the user to recreate work that is
		// still there. Same rule the draft's persistence holds, asserted here for the named path.
		const { store } = fakeStore([], { unreadable: true });
		const dock = new DockViews(store, () => ({ g: 1 }));
		await dock.refresh();

		expect(dock.phase).toBe('unreadable');
		expect(dock.detail).toBe('boom');
		expect(dock.views).toEqual([]);
	});

	it('refuses every write while the library is unreadable', async () => {
		const { store, peek } = fakeStore([], { unreadable: true });
		const dock = new DockViews(store, () => ({ g: 1 }));
		await dock.refresh();

		expect(await dock.saveAs('X')).toBe(false);
		expect(await dock.save()).toBe(false);
		expect(await dock.rename('v1', 'Y')).toBe(false);
		expect(await dock.remove('v1')).toBe(false);
		expect(peek()).toEqual([]);
	});

	it('a refused write leaves the local list untouched', async () => {
		const { store } = fakeStore([view('v1', 'A', 1)], { refuse: true });
		const dock = new DockViews(store, () => ({ g: 1 }));
		await dock.refresh();

		expect(await dock.remove('v1')).toBe(false);
		expect(dock.views).toHaveLength(1);
	});
});
