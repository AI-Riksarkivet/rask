/**
 * WHERE the workbench keeps a person's layout and named views — the one module that decides it.
 *
 * In this browser's `localStorage`, one key per signed-in person: `rask-workbench:` and the SHA-256 of
 * the principal's id, so the key never carries the id itself; on a stack that signs nobody in, one
 * fixed key. The value is the last layout and the named views, and nothing else: a layout names only
 * panel ids, a view adds the name the person gave it. No token and no lakehouse data are stored.
 *
 * Sign-out does not clear it, unlike the activity log (`activity.forget()`): a layout is a preference
 * of the person who signs in on this browser again, while the activity log records what they did.
 *
 * Every access is guarded: where storage is refused (a private window, blocked site data, a full
 * quota) the document lives in memory for the page's life, and `persistent` says so.
 *
 * Reads keep three outcomes. `absent`: nothing stored. `ok`. `unreadable`: something is stored and
 * could not be read; it is reported, never replaced by a default, and never written over until the
 * person resets it.
 */
import type { LayoutConfig } from 'horizon-layout';
import { readStoredLayout, type LayoutRead } from './layout.ts';

const PREFIX = 'rask-workbench:';
const ANONYMOUS = `${PREFIX}local`;

/** One saved view. `id` is stable across renames; the sidebar addresses views by it. */
export interface StoredView {
	readonly id: string;
	readonly name: string;
	/** Read with `readStoredLayout` when applied: a view is a stored layout like any other. */
	readonly layout: unknown;
	/** ISO-8601, the order the sidebar lists views in. */
	readonly updated: string;
}

export type ViewsRead =
	| { readonly status: 'ok'; readonly views: StoredView[] }
	| { readonly status: 'unreadable'; readonly detail: string };

/** The document under the key. `layout` is kept as stored, so writing views never rewrites it. */
interface Document {
	layout?: unknown;
	views?: unknown;
}

function isView(value: unknown): value is StoredView {
	if (typeof value !== 'object' || value === null) return false;
	const v = value as Record<string, unknown>;
	return typeof v.id === 'string' && typeof v.name === 'string' && typeof v.updated === 'string';
}

type Read = { readonly doc: Document } | { readonly broken: string };

export class WorkbenchStore {
	readonly #key: string;
	readonly #storage: () => Storage;
	/** The document as this page last read or wrote it; the record itself while storage is refused. */
	#doc: Document = {};
	#persistent = true;
	/** Told after this page's own reset, which another tab hears as a `storage` event. */
	readonly #resetListeners = new Set<() => void>();

	constructor(key: string, storage: () => Storage) {
		this.#key = key;
		this.#storage = storage;
		this.#read();
	}

	/** False when this browser refuses storage: the layout and views last until the tab closes. */
	get persistent(): boolean {
		return this.#persistent;
	}

	/**
	 * The stored document as it is NOW. Read on every load and before every write, because another
	 * tab of the same person writes the same key: a write merges into what is stored, never into what
	 * this page read earlier.
	 */
	#read(): Read {
		if (!this.#persistent) return { doc: this.#doc };
		let raw: string | null;
		try {
			raw = this.#storage().getItem(this.#key);
		} catch {
			this.#persistent = false;
			return { doc: this.#doc };
		}
		if (raw === null) return { doc: {} };
		try {
			const parsed: unknown = JSON.parse(raw);
			if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
				throw new Error('not an object');
			}
			this.#doc = parsed as Document;
			return { doc: this.#doc };
		} catch (e) {
			return {
				broken: `the stored workbench is not JSON this console can read (${e instanceof Error ? e.message : String(e)})`,
			};
		}
	}

	loadLayout(): LayoutRead {
		const read = this.#read();
		if ('broken' in read) return { status: 'unreadable', detail: read.broken };
		if (read.doc.layout === undefined) return { status: 'absent' };
		return readStoredLayout(read.doc.layout);
	}

	loadViews(): ViewsRead {
		const read = this.#read();
		if ('broken' in read) return { status: 'unreadable', detail: read.broken };
		const views = read.doc.views ?? [];
		if (!Array.isArray(views) || !views.every(isView)) {
			return { status: 'unreadable', detail: 'the stored views are not a list of named layouts' };
		}
		return { status: 'ok', views };
	}

	/** Store the live layout; the stored views are kept as they are. Refused over a document that is
	 *  not JSON. */
	saveLayout(layout: LayoutConfig): boolean {
		return this.#update((doc) => ({ ...doc, layout }));
	}

	/** Store the named views; the stored layout is kept as it is. */
	saveViews(views: readonly StoredView[]): boolean {
		return this.#update((doc) => ({ ...doc, views: [...views] }));
	}

	/**
	 * The person's explicit reset of the layout: the stored layout becomes `layout`. A document that is
	 * not JSON is replaced whole, its views with it, since nothing in it could be shown.
	 */
	reset(layout: LayoutConfig): boolean {
		return this.#update((doc) => ({ ...doc, layout }), true);
	}

	/** The person's explicit reset of the named views: none are kept, and the layout stays. */
	resetViews(): boolean {
		return this.#update((doc) => ({ ...doc, views: [] }), true);
	}

	/**
	 * Call `listener` when this person's document changes under the page: another tab wrote it, or
	 * this page reset it. Returns the unsubscribe.
	 */
	onChange(listener: () => void): () => void {
		this.#resetListeners.add(listener);
		if (typeof window === 'undefined') return () => this.#resetListeners.delete(listener);
		const handle = (event: StorageEvent): void => {
			if (event.key === this.#key || event.key === null) listener();
		};
		window.addEventListener('storage', handle);
		return () => {
			this.#resetListeners.delete(listener);
			window.removeEventListener('storage', handle);
		};
	}

	#update(change: (doc: Document) => Document, isReset = false): boolean {
		const read = this.#read();
		if ('broken' in read && !isReset) return false;
		const next = change('broken' in read ? {} : read.doc);
		this.#doc = next;
		if (this.#persistent) {
			try {
				this.#storage().setItem(this.#key, JSON.stringify(next));
			} catch {
				// Refused now (a full quota, site data blocked mid-session): the page keeps working from
				// memory, and says so.
				this.#persistent = false;
			}
		}
		if (isReset) for (const listener of this.#resetListeners) listener();
		return true;
	}
}

async function keyFor(principal: string | null): Promise<string> {
	if (principal === null) return ANONYMOUS;
	const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(principal));
	const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
	return `${PREFIX}${hex}`;
}

/** The store for one person (`principal`, the signed-in subject; null where nobody signs in). */
export async function openWorkbenchStore(
	principal: string | null,
	storage: () => Storage = () => window.localStorage,
): Promise<WorkbenchStore> {
	try {
		return new WorkbenchStore(await keyFor(principal), storage);
	} catch {
		// No hash to key by (an insecure context has no `crypto.subtle`): memory only.
		return new WorkbenchStore(ANONYMOUS, () => {
			throw new Error('no storage key');
		});
	}
}

/** The named views' operations, as the sidebar's state model calls them. */
export interface ViewsStore {
	list(): Promise<ViewsRead>;
	/** Create a view, newest first. Returns the stored record, or null when the write was refused. */
	create(name: string, layout: LayoutConfig): Promise<StoredView | null>;
	update(id: string, layout: LayoutConfig): Promise<boolean>;
	rename(id: string, name: string): Promise<boolean>;
	remove(id: string): Promise<boolean>;
	/** The person's explicit reset of views that could not be read: none are kept. */
	reset(): Promise<boolean>;
}

const newId = (): string =>
	typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

/** The views of `store`, each change a read-modify-write that refuses while the views are unreadable. */
export function viewsOf(
	store: WorkbenchStore,
	now = (): string => new Date().toISOString(),
): ViewsStore {
	const mutate = async (fn: (views: StoredView[]) => StoredView[]): Promise<boolean> => {
		const read = store.loadViews();
		if (read.status === 'unreadable') return false;
		return store.saveViews(fn(read.views));
	};
	return {
		list: async () => store.loadViews(),
		async create(name, layout) {
			const view: StoredView = { id: newId(), name, layout, updated: now() };
			return (await mutate((views) => [view, ...views])) ? view : null;
		},
		update: (id, layout) =>
			mutate((views) => views.map((v) => (v.id === id ? { ...v, layout, updated: now() } : v))),
		rename: (id, name) =>
			mutate((views) => views.map((v) => (v.id === id ? { ...v, name, updated: now() } : v))),
		remove: (id) => mutate((views) => views.filter((v) => v.id !== id)),
		reset: async () => store.resetViews(),
	};
}
