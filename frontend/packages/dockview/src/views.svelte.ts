/**
 * The saved-views state model — the list, WHICH ONE IS ACTIVE, and whether the live arrangement has
 * DIVERGED from it. All three readable by the UI without polling.
 *
 * THE DECISION THE BACKLOG SKIPPED, made here and pinned by tests:
 *
 *   Once a view is loaded and the user drags a sash, autosave keeps writing the implicit DRAFT. The
 *   named view stays frozen until an explicit Save.
 *
 * The two alternatives are both worse and both silent. Overwriting the view on every drag means you
 * can never open a saved arrangement to look at it without destroying it — the exact opposite of what
 * saving one is for. Silently forking (auto-creating "Compare two runs (2)") fills the sidebar with
 * layouts nobody asked for and makes the name you chose stop meaning anything. So: the draft absorbs
 * every change, the view is a snapshot, and the gap between them is shown rather than resolved
 * behind the user's back — `dirty` is that gap, and the UI renders it as a modified marker beside the
 * active view's name.
 *
 * `dirty` is computed by comparing the live layout against the loaded snapshot rather than by setting
 * a flag on every mutation. A flag would drift the moment anything changed a layout without going
 * through this class — and the honest question is not "did something happen?" but "does what is on
 * screen still match what was saved?", which only a comparison can answer. Serialized layouts are
 * plain JSON of bounded size, so the comparison is cheap and, unlike a flag, self-correcting: drag a
 * panel away and back, and the marker clears itself.
 */
import type { DockView, DockViewsStore } from '@rask/api/dock-views';
import type { LayoutRead } from './types';

/** JSON with SORTED object keys, recursively — the divergence baseline's serializer. dockview's
 *  toJSON/fromJSON round-trip reorders the `panels` record's keys, so plain JSON.stringify marked an
 *  untouched, freshly-applied view as modified (review finding). Key order is not layout. */
function stableStringify(value: unknown): string {
	return JSON.stringify(value, (_key, v: unknown) => {
		if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
			const rec = v as Record<string, unknown>;
			return Object.fromEntries(
				Object.keys(rec)
					.sort()
					.map((k) => [k, rec[k]]),
			);
		}
		return v;
	});
}

/** What the UI needs from a view row, without exposing the layout payload to it. */
export interface ViewSummary {
	readonly id: string;
	readonly name: string;
	readonly updated: string;
}

export type ViewsPhase = 'loading' | 'ready' | 'unreadable';

export class DockViews<T> {
	#store: DockViewsStore<T>;
	/** Reads the dock's CURRENT arrangement. Injected so this class needs no DockviewApi. */
	#snapshot: () => T | null;

	#views = $state<DockView<T>[]>([]);
	#activeId = $state<string | null>(null);
	/** The layout as it was when the active view was loaded or last saved — the divergence baseline. */
	#baseline = $state<string | null>(null);
	/** Bumped whenever the dock's layout changes, so `dirty` re-derives. */
	#revision = $state(0);
	#phase = $state<ViewsPhase>('loading');
	#detail = $state<string | null>(null);
	#busy = $state(false);
	/** The last refused write, or null — the sidebar renders it instead of failing silently. */
	#lastError = $state<string | null>(null);

	constructor(store: DockViewsStore<T>, snapshot: () => T | null) {
		this.#store = store;
		this.#snapshot = snapshot;
	}

	/** Every saved view, newest first. Layout payloads stay inside — the sidebar renders summaries. */
	get views(): ViewSummary[] {
		return this.#views.map(({ id, name, updated }) => ({ id, name, updated }));
	}

	get activeId(): string | null {
		return this.#activeId;
	}

	get active(): ViewSummary | null {
		const v = this.#views.find((x) => x.id === this.#activeId);
		return v === undefined ? null : { id: v.id, name: v.name, updated: v.updated };
	}

	get phase(): ViewsPhase {
		return this.#phase;
	}

	/** Why the library could not be read. Shown rather than swallowed — see `unreadable`. */
	get detail(): string | null {
		return this.#detail;
	}

	/** True while a write is in flight, so the UI can disable the controls that would race it. */
	get busy(): boolean {
		return this.#busy;
	}

	/** The last refused write's description, cleared by the next success. Review finding: every
	 *  refused save/rename/delete was a silent no-op — the name input just sat there. */
	get lastError(): string | null {
		return this.#lastError;
	}

	/**
	 * Has the live arrangement moved away from the active view?
	 *
	 * False when no view is active: the draft is not "dirty" against nothing, it is simply the draft.
	 */
	get dirty(): boolean {
		if (this.#activeId === null || this.#baseline === null) return false;
		// `#revision` is read so this re-derives when `touch()` fires; the comparison is the answer.
		void this.#revision;
		const live = this.#snapshot();
		return live !== null && stableStringify(live) !== this.#baseline;
	}

	/**
	 * The dock's layout changed. Wire to `onDidLayoutChange`.
	 *
	 * Deliberately NOT debounced, unlike autosave: this writes no bytes, and a modified marker that
	 * appears a second after the drag reads as a bug.
	 */
	touch(): void {
		this.#revision += 1;
	}

	/** Load the library. Safe to call again — a refresh keeps the active selection if it survived. */
	async refresh(): Promise<void> {
		const read = await this.#store.list();
		if (read.status === 'unreadable') {
			// Surfaced, never treated as empty: reporting an unreadable library as "no saved views" would
			// invite the user to recreate work that is still there.
			this.#phase = 'unreadable';
			this.#detail = read.detail;
			return;
		}
		this.#views = read.status === 'ok' ? read.layout : [];
		this.#phase = 'ready';
		this.#detail = null;
		if (this.#activeId !== null && !this.#views.some((v) => v.id === this.#activeId)) {
			this.clearActive();
		}
	}

	/**
	 * The layout to apply for a view, as the dock's own three-outcome contract.
	 *
	 * Returns `LayoutRead` rather than a bare layout so a missing view is `absent` rather than a thrown
	 * error — a stale sidebar click is an ordinary thing, not a crash.
	 */
	select(id: string): LayoutRead {
		// READ-ONLY on purpose: marking the view active here meant a click while the dock was still
		// loading — or an apply that threw — left a checked, never-applied view (review finding).
		// The caller applies the layout first, then commits with `activate(id)`.
		const view = this.#views.find((v) => v.id === id);
		if (view === undefined) return { status: 'absent' };
		return { status: 'ok', layout: view.layout as never };
	}

	/** Commit a view as ACTIVE — call after its layout was actually applied to the dock. */
	activate(id: string): void {
		const view = this.#views.find((v) => v.id === id);
		if (view === undefined) return;
		this.#activeId = id;
		this.#baseline = stableStringify(view.layout);
		this.#revision += 1;
	}

	/** Stop tracking a view — the arrangement stays exactly as it is, it just stops being "the view". */
	clearActive(): void {
		this.#activeId = null;
		this.#baseline = null;
	}

	/** Save the current arrangement as a NEW view, and make it active. */
	async saveAs(name: string): Promise<boolean> {
		const layout = this.#snapshot();
		if (layout === null || name.trim() === '') return false;
		return this.#write('Save view', async () => {
			const created = await this.#store.create(name.trim(), layout);
			if (created === null) return false;
			this.#views = [created, ...this.#views];
			this.#activeId = created.id;
			this.#baseline = stableStringify(layout);
			return true;
		});
	}

	/** Overwrite the ACTIVE view with the current arrangement — the explicit Save `dirty` invites. */
	async save(): Promise<boolean> {
		const id = this.#activeId;
		const layout = this.#snapshot();
		if (id === null || layout === null) return false;
		return this.#write('Save', async () => {
			if (!(await this.#store.update(id, layout))) return false;
			this.#views = this.#views.map((v) => (v.id === id ? { ...v, layout } : v));
			this.#baseline = stableStringify(layout);
			return true;
		});
	}

	async rename(id: string, name: string): Promise<boolean> {
		if (name.trim() === '') return false;
		return this.#write('Rename', async () => {
			if (!(await this.#store.rename(id, name.trim()))) return false;
			this.#views = this.#views.map((v) => (v.id === id ? { ...v, name: name.trim() } : v));
			return true;
		});
	}

	async remove(id: string): Promise<boolean> {
		return this.#write('Delete', async () => {
			if (!(await this.#store.remove(id))) return false;
			this.#views = this.#views.filter((v) => v.id !== id);
			// Deleting the active view leaves the arrangement on screen untouched — it is now the draft
			// again. Clearing the dock would destroy work the user never asked to lose.
			if (this.#activeId === id) this.clearActive();
			return true;
		});
	}

	/** One busy flag, one refusal rule: nothing writes while the library is unreadable. */
	async #write(op: string, fn: () => Promise<boolean>): Promise<boolean> {
		if (this.#phase === 'unreadable') {
			this.#lastError = `${op} refused — the library is unreadable`;
			return false;
		}
		if (this.#busy) return false;
		this.#busy = true;
		try {
			const ok = await fn();
			this.#lastError = ok ? null : `${op} failed — not saved (offline or session expired?)`;
			return ok;
		} finally {
			this.#busy = false;
		}
	}
}
