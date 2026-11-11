/**
 * The workbench page's setup: open the person's store, restore the layout, list the named views, and
 * keep both in step with what is stored while the page lives.
 *
 * A module rather than the page's `onMount`, so the two ways it can go wrong are testable without a
 * browser: the page leaving before the store has opened (opening waits on `crypto.subtle`), and the
 * stored layout changing under the page (another tab, or a reset of the whole document).
 */
import type { LayoutConfig } from 'horizon-layout';
import { LayoutAutosave } from './autosave.ts';
import type { LayoutRead } from './layout.ts';
import { viewsOf, type WorkbenchStore } from './store.ts';
import { DockViews } from './views.svelte.ts';

export interface WorkbenchSession {
	readonly store: WorkbenchStore;
	readonly autosave: LayoutAutosave;
	readonly views: DockViews;
	/** The stored layout as the page opened: `ok`, `absent`, or `unreadable` with why. */
	readonly restored: LayoutRead;
	/** The page is leaving: stop listening and write what is pending. */
	stop(): void;
}

export interface SessionHooks {
	/** The dock's live layout, for the views' modified marker and a new view's snapshot. */
	snapshot: () => LayoutConfig | null;
	/**
	 * The stored layout as it is now, after the page last read it: `unreadable` when a save found it
	 * so or another tab broke it, readable again when a reset or another tab replaced it.
	 * `wasRefused` says saving was off until now, so the page can show what is stored instead of its
	 * default.
	 */
	onLayout: (read: LayoutRead, wasRefused: boolean) => void;
	/**
	 * The session, handed over in the same turn its listeners are added. A promise's continuation
	 * runs a microtask later, and a page torn down in that gap would hold nothing to stop.
	 */
	started: (session: WorkbenchSession) => void;
}

/** Start the workbench over the store `opening` resolves to; nothing starts when the page `left` meanwhile. */
export async function startWorkbench(
	opening: Promise<WorkbenchStore>,
	hooks: SessionHooks,
	left: () => boolean,
): Promise<void> {
	const store = await opening;
	// Nothing to start for a page that is gone: a listener added now would outlive it.
	if (left()) return;

	const autosave = new LayoutAutosave(store, undefined, (detail) =>
		hooks.onLayout({ status: 'unreadable', detail }, false),
	);
	const restored = autosave.restore();
	const views = new DockViews(viewsOf(store), hooks.snapshot);
	void views.refresh();

	const stopWatching = store.onChange(() => {
		const wasRefused = !autosave.saving;
		void views.refresh();
		hooks.onLayout(autosave.recheck(), wasRefused);
	});

	hooks.started({
		store,
		autosave,
		views,
		restored,
		stop() {
			stopWatching();
			autosave.dispose();
		},
	});
}
