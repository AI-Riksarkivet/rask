/**
 * The workbench's live arrangement, restored once and autosaved after every change.
 *
 * Two rules carry this file, and both are about not destroying a workspace:
 *
 * 1. An unreadable layout is never overwritten. When the stored layout exists and cannot be read
 *    (not JSON, or it fails `parseLayoutConfig` / `validateConfig`), saving is off: the dock shows
 *    the default arrangement and no change to it is stored, until the person resets the layout.
 * 2. A drag is one write. A resize changes the layout on every pointer move, so saves are debounced
 *    to the trailing edge, and a save still pending when the page goes is written then (`flush`).
 */
import type { LayoutConfig } from 'horizon-layout';
import type { LayoutRead } from './layout.ts';

/** What the autosave needs from the store: one read, one write, and the explicit reset. */
export interface LayoutStore {
	loadLayout(): LayoutRead;
	saveLayout(layout: LayoutConfig): boolean;
	reset(layout: LayoutConfig): boolean;
}

export class LayoutAutosave {
	#store: LayoutStore;
	#debounceMs: number;
	#timer: ReturnType<typeof setTimeout> | null = null;
	/** The layout the pending timer will write. */
	#pending: LayoutConfig | null = null;
	#refuseToSave = false;
	/** Told why, when a save finds the stored layout unreadable. */
	#onRefused: (detail: string) => void;

	constructor(
		store: LayoutStore,
		debounceMs = 400,
		onRefused: (detail: string) => void = () => {},
	) {
		this.#store = store;
		this.#debounceMs = debounceMs;
		this.#onRefused = onRefused;
	}

	/** Read the stored layout; `unreadable` turns saving off. */
	restore(): LayoutRead {
		return this.recheck();
	}

	/**
	 * Read the stored layout again and save or refuse by what it is now: another tab or a views reset
	 * may have replaced it, or made it unreadable, since this page last read it.
	 */
	recheck(): LayoutRead {
		const read = this.#store.loadLayout();
		this.#refuseToSave = read.status === 'unreadable';
		return read;
	}

	/** Whether changes are being saved; false while a stored layout could not be read. */
	get saving(): boolean {
		return !this.#refuseToSave;
	}

	/** The layout changed: store it after the debounce, unless saving is off. */
	schedule(layout: LayoutConfig): void {
		if (this.#refuseToSave) return;
		if (this.#timer !== null) clearTimeout(this.#timer);
		this.#pending = layout;
		this.#timer = setTimeout(() => this.flush(), this.#debounceMs);
	}

	/** Write a pending save now: the page is leaving, or the debounce ran out. */
	flush(): void {
		if (this.#timer !== null) clearTimeout(this.#timer);
		this.#timer = null;
		const layout = this.#pending;
		this.#pending = null;
		if (layout === null || this.#refuseToSave) return;
		if (this.#store.saveLayout(layout)) return;
		// Refused: the stored document became unreadable since it was read. Saving stops, and the page
		// is told why, rather than a change silently not being kept.
		const read = this.recheck();
		if (read.status === 'unreadable') this.#onRefused(read.detail);
	}

	/** The person chose to replace a stored layout that could not be read: store `layout`, save again. */
	reset(layout: LayoutConfig): void {
		if (this.#store.reset(layout)) this.#refuseToSave = false;
	}

	/** The workbench is leaving: what is pending is written, not dropped. */
	dispose(): void {
		this.flush();
	}
}
