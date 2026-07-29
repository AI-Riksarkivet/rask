/**
 * The saved-views library — named dock layouts, on their own per-subject document.
 *
 * A SEPARATE document from `dock-layout` (see `dock-layout.ts`), never a key inside it. The server's
 * `DockLayouts` is `extra="forbid"`, so a client that does not know about a sibling key must DROP it —
 * one autosave from an older bundle would erase every saved view. The byte ceiling and the 409 are per
 * document too, so a large or corrupt library cannot take the implicit autosave down with it.
 *
 * THE DIVISION THIS FILE ASSUMES, stated once because everything else follows from it:
 *
 *   - The DRAFT is the live arrangement, autosaved on every change into `dock-layout`. Unchanged by
 *     this feature — not one line of `dock-layout.ts` or `persistence.ts` moves.
 *   - A VIEW is a named snapshot, written only by an explicit user act.
 *
 * So loading a view does NOT redirect autosave at it. Autosave keeps writing the draft, the view stays
 * frozen until you press Save, and the UI shows a modified marker in between. The alternative —
 * autosave overwriting the named view — means you can never open a saved arrangement to look at it
 * without destroying it, which is the opposite of what saving one is for.
 *
 * Generic over the layout type for the same reason `dock-layout.ts` is: `@rask/api` is the transport
 * layer and must not depend on a UI package, so it never names `SerializedDockview`.
 */
import type { StoredLayoutRead } from './dock-layout';

/** One saved view. `id` is stable across renames; the UI addresses views by it, never by name. */
export interface DockView<T> {
	readonly id: string;
	readonly name: string;
	readonly layout: T;
	/** ISO-8601, stamped by the client — the same clock that orders the list. */
	readonly updated: string;
}

/** The document as the catalog serves it — mirrors `service_kit.schemas.dock_layout.DockLayoutLibrary`. */
interface LibraryDocument<T> {
	workbenches: Record<string, { views: DockView<T>[] }>;
}

interface Envelope<T> {
	exists?: boolean;
	value?: LibraryDocument<T>;
}

export interface DockViewsStore<T> {
	/** Every saved view for this workbench, in sidebar order. Three outcomes, like every read here. */
	list(): Promise<StoredLayoutRead<DockView<T>[]>>;
	/** Create a view. Returns the stored record, or null when the write was refused. */
	create(name: string, layout: T): Promise<DockView<T> | null>;
	/** Overwrite an existing view's layout — the explicit Save that a modified marker invites. */
	update(id: string, layout: T): Promise<boolean>;
	rename(id: string, name: string): Promise<boolean>;
	remove(id: string): Promise<boolean>;
}

export interface DockViewsStoreOptions {
	/** Namespaces this workbench inside the ONE per-subject library, so zones cannot collide. */
	workbenchId: string;
	/** The zone's own path to the catalog's library document. */
	endpoint: string;
	/** Injected for tests and for server-side reads. */
	fetcher?: typeof fetch;
	/** Injected for tests — ids and timestamps are the only non-determinism here. */
	now?: () => string;
	newId?: () => string;
}

/** Crypto-random where available, falling back to something still collision-safe in practice. */
const defaultId = (): string =>
	typeof crypto !== 'undefined' && 'randomUUID' in crypto
		? crypto.randomUUID()
		: `v-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;

export function makeDockViewsStore<T>(options: DockViewsStoreOptions): DockViewsStore<T> {
	const { workbenchId, endpoint } = options;
	const fetcher = options.fetcher ?? fetch;
	const now = options.now ?? (() => new Date().toISOString());
	const newId = options.newId ?? defaultId;

	/** The whole document, as one of the three outcomes. Shared by every operation below. */
	async function read(): Promise<StoredLayoutRead<LibraryDocument<T>>> {
		let response: Response;
		try {
			response = await fetcher(endpoint);
		} catch (e) {
			// Unreachable is NOT absent. Guessing "you have no saved views" is the guess that lets the
			// next write replace a library that is still there.
			return {
				status: 'unreadable',
				detail: e instanceof Error ? e.message : 'the view library is unreachable',
			};
		}
		if (response.status === 409) {
			const detail = await response
				.json()
				.then((b: { detail?: string }) => b.detail ?? 'the stored view library cannot be read')
				.catch(() => 'the stored view library cannot be read');
			return { status: 'unreadable', detail };
		}
		// 401 is an auth-off dev stack or an expired session. Unlike the draft, there is no localStorage
		// mirror here on purpose: a named view is a deliberate act and must live where it can be shared
		// with the user's other machines, or it is not saved at all.
		if (response.status === 401) return { status: 'absent' };
		if (!response.ok) {
			return { status: 'unreadable', detail: `the view library answered ${response.status}` };
		}
		const body = (await response.json()) as Envelope<T>;
		if (body.exists !== true || body.value === undefined) return { status: 'absent' };
		const w = body.value.workbenches;
		if (typeof w !== 'object' || w === null) {
			return { status: 'unreadable', detail: 'the stored library has no workbenches map' };
		}
		return { status: 'ok', layout: body.value };
	}

	/**
	 * Read-modify-write, never a blind PUT: ONE document holds every workbench's views, so replacing it
	 * wholesale would delete the other zones' libraries.
	 *
	 * Refuses on `unreadable` — the same rule `persistence.ts` holds for the draft, and for the same
	 * reason: a document that exists but could not be read must never be overwritten by a client that
	 * has no idea what was in it.
	 */
	async function mutate(fn: (views: DockView<T>[]) => DockView<T>[]): Promise<boolean> {
		const doc = await read();
		if (doc.status === 'unreadable') return false;
		const existing = doc.status === 'ok' ? doc.layout.workbenches : {};
		const next: LibraryDocument<T> = {
			workbenches: {
				...existing,
				[workbenchId]: { views: fn(existing[workbenchId]?.views ?? []) },
			},
		};
		try {
			const response = await fetcher(endpoint, {
				method: 'PUT',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify(next),
			});
			return response.ok;
		} catch {
			return false;
		}
	}

	return {
		async list(): Promise<StoredLayoutRead<DockView<T>[]>> {
			const doc = await read();
			if (doc.status === 'unreadable') return doc;
			if (doc.status === 'absent') return { status: 'ok', layout: [] };
			// This workbench missing from an otherwise-fine document is genuinely empty, not unreadable.
			return { status: 'ok', layout: doc.layout.workbenches[workbenchId]?.views ?? [] };
		},

		async create(name: string, layout: T): Promise<DockView<T> | null> {
			const view: DockView<T> = { id: newId(), name, layout, updated: now() };
			// Newest first — the sidebar's order is the document's order.
			const ok = await mutate((views) => [view, ...views]);
			return ok ? view : null;
		},

		update(id: string, layout: T): Promise<boolean> {
			return mutate((views) =>
				views.map((v) => (v.id === id ? { ...v, layout, updated: now() } : v)),
			);
		},

		rename(id: string, name: string): Promise<boolean> {
			return mutate((views) =>
				views.map((v) => (v.id === id ? { ...v, name, updated: now() } : v)),
			);
		},

		remove(id: string): Promise<boolean> {
			return mutate((views) => views.filter((v) => v.id !== id));
		},
	};
}
