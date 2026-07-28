/**
 * Debounced layout autosave over an injected {@link LayoutStore}.
 *
 * Two rules carry this file, and both are about not destroying a workspace:
 *
 * 1. **An unreadable layout is never overwritten.** If `load()` answers `unreadable` — the stored
 *    document exists but could not be parsed, or the store is unreachable — saving is DISABLED for
 *    the lifetime of this controller. Treating unreadable as absent is the bug: the dock would seed
 *    a default layout and the first sash drag would overwrite a workspace that is still there. The
 *    catalog answers 409 for exactly this case; refusing to write is the client half of that.
 *
 * 2. **A drag is one write, not two hundred.** `onDidLayoutChange` is documented as "an aggregation
 *    of many events" and fires throughout a sash drag, so every save is debounced to the trailing
 *    edge. `onDidLayoutChange` is used rather than `onDidMutateLayout` on purpose: the mutation
 *    event covers add/remove/move/float but NOT a resize, and pane sizes are the part users most
 *    notice losing.
 */
// `DockviewIDisposable`, not `IDisposable` — dockview prefixes its Event/Emitter/Disposable trio
// deliberately (see the note in renderer.svelte.ts).
import type { DockviewApi, DockviewIDisposable } from 'dockview';
import type { LayoutStore } from './types';

export class LayoutAutosave implements DockviewIDisposable {
	#store: LayoutStore;
	#api: DockviewApi;
	#debounceMs: number;
	#timer: ReturnType<typeof setTimeout> | null = null;
	#subscription: DockviewIDisposable | null = null;
	/** Set when the stored layout could not be read. While true, every save is refused. */
	#refuseToSave = false;

	constructor(store: LayoutStore, api: DockviewApi, debounceMs: number) {
		this.#store = store;
		this.#api = api;
		this.#debounceMs = debounceMs;
	}

	/**
	 * Read the stored layout and apply it. Returns whether a layout was restored, so the caller can
	 * seed defaults only when there genuinely was none.
	 *
	 * A layout that fails to deserialize is caught rather than thrown: a corrupt document must leave
	 * the user with an empty dock they can rebuild, not a blank page. Saving stays disabled in that
	 * case, for the same reason as an unreadable read.
	 */
	async restore(): Promise<boolean> {
		const read = await this.#store.load();
		if (read.status === 'unreadable') {
			this.#refuseToSave = true;
			return false;
		}
		if (read.status === 'absent') return false;
		try {
			this.#api.fromJSON(read.layout);
			return true;
		} catch {
			this.#refuseToSave = true;
			this.#api.clear();
			return false;
		}
	}

	/** Begin watching. Call AFTER restore + any default seeding, so neither triggers a write. */
	start(): void {
		this.#subscription = this.#api.onDidLayoutChange(() => this.#schedule());
	}

	dispose(): void {
		if (this.#timer !== null) clearTimeout(this.#timer);
		this.#timer = null;
		this.#subscription?.dispose();
		this.#subscription = null;
	}

	#schedule(): void {
		if (this.#refuseToSave) return;
		if (this.#timer !== null) clearTimeout(this.#timer);
		this.#timer = setTimeout(() => {
			this.#timer = null;
			// Serializing can throw while the grid is mid-mutation; a failed autosave must never
			// surface as an unhandled rejection over the user's work.
			try {
				void this.#store.save(this.#api.toJSON()).catch(() => {});
			} catch {
				/* the next layout change will try again */
			}
		}, this.#debounceMs);
	}
}
