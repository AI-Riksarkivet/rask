/**
 * Reactive saved-views store — named read-plane selections, kept per SUBJECT on the server.
 *
 * These used to live in `localStorage`, so the same signed-in person on another machine found none of
 * their saved searches. They now belong to the user rather than to the browser: `$lib/user-state` reads
 * and writes them through this zone's `capi/v1/user-state/saved-views` proxy, and `localStorage` survives
 * only as a mirror for an auth-off dev stack and an offline tab.
 *
 * The one rule that matters: a document we could not READ is never overwritten. `ready` stays false and
 * every save is refused while `unreadable` holds, because a store answering "cannot read" and a store
 * answering "nothing saved" look identical on an empty list — and only one of them means the user's views
 * are still there. Thin runes wrapper over the pure logic in `saved-views.ts`, as before.
 */
import type { SearchSpec } from '@rask/media-api';
import {
	removeView,
	stripEphemeral,
	upsertView,
	viewsForDataset,
	type SavedView,
} from '$lib/saved-views';
import { readUserState, writeUserState } from '$lib/user-state';

class SavedViewsStore {
	views = $state<SavedView[]>([]);
	/** False until a read settles. A save before that could overwrite what the read is about to return. */
	ready = $state(false);
	/** Why the server's copy could not be read, when it could not. Saving is refused while this is set. */
	unreadable = $state<string | null>(null);

	/** The read in flight, if one is. Two callers awaiting the same read is one request, not two. */
	#loading: Promise<void> | null = null;

	/**
	 * Load the caller's views. Safe to call from an `$effect` and safe to call concurrently.
	 *
	 * The effect that calls this reads `ready` and `unreadable` in its own guard, and `load` assigns
	 * both — so the guard cannot be what serialises the calls: it only takes effect one microtask AFTER
	 * the read settles. Two components mounting the popover in the same tick would each see the guard
	 * open and issue a full GET. De-duplicating here makes that structurally impossible rather than
	 * timing-dependent, which is the svelte MCP autofixer's point about calling functions in effects.
	 */
	load(): Promise<void> {
		this.#loading ??= this.#read().finally(() => {
			this.#loading = null;
		});
		return this.#loading;
	}

	async #read(): Promise<void> {
		const got = await readUserState<SavedView[]>('saved-views');
		if (got.status === 'unreadable') {
			// Deliberately leaves `ready` false: the list stays empty AND unsaveable, so a user sees their
			// views are missing rather than silently building a new set on top of the old one.
			this.unreadable = got.detail;
			return;
		}
		this.unreadable = null;
		// Guard SHAPE, not just JSON validity: a stored `null`/`{}`/`42` parses fine but would make
		// `views` a non-array — a `.filter` crash on first render.
		this.views = got.status === 'ok' && Array.isArray(got.value) ? got.value : [];
		this.ready = true;
	}

	forDataset(dataset: string): SavedView[] {
		return viewsForDataset(this.views, dataset);
	}

	/** Save (or overwrite) a named view of the current spec for a dataset. */
	async save(name: string, spec: SearchSpec, dataset: string): Promise<void> {
		const trimmed = name.trim();
		if (!trimmed || !this.ready) return;
		this.views = upsertView(this.views, { name: trimmed, dataset, spec: stripEphemeral(spec) });
		await this.#persist();
	}

	async remove(name: string, dataset: string): Promise<void> {
		if (!this.ready) return;
		this.views = removeView(this.views, name, dataset);
		await this.#persist();
	}

	async #persist(): Promise<void> {
		// A refused write is reported, not swallowed. The previous store swallowed a full localStorage and
		// left the user believing a view had been saved.
		if (!(await writeUserState('saved-views', this.views))) {
			this.unreadable = 'the last change could not be saved — the store refused the write';
		}
	}
}

export const savedViews = new SavedViewsStore();
