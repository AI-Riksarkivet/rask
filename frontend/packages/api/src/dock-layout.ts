/**
 * The estate's ONE dock-layout store — per-subject, on the catalog's `dock-layout` user-state
 * document, backed by the Dapr state store.
 *
 * This started as a copy in the lakehouse and a near-identical copy in media, which is exactly the
 * drift the shared data layer exists to prevent: two implementations of a contract whose whole point
 * is that getting it wrong destroys a user's saved work. There is one here now, and a zone supplies
 * only what is genuinely zone-shaped — the endpoint its own BFF serves.
 *
 * **Generic over the layout type on purpose.** `@rask/api` is the transport layer and must not depend
 * on a UI package, so it never names `SerializedDockview`; `@rask/dockview`'s `LayoutStore` is
 * satisfied structurally by {@link StoredLayoutStore}<SerializedDockview>. Same stance as `@rask/ui`
 * declaring its client props structurally rather than importing them.
 *
 * **The three outcomes, and why the middle one cannot be collapsed:**
 *   - `ok`         — a layout came back. Use it.
 *   - `absent`     — the user genuinely has none. Seed defaults; saving is safe.
 *   - `unreadable` — one EXISTS and could not be read (schema drift, owner mismatch, store down). Do
 *                    NOT seed and do NOT save. Treat it as empty and the next autosave overwrites a
 *                    workspace that is still there. The catalog answers 409 rather than
 *                    `exists: false` for exactly this; refusing to write over it is the client half.
 *
 * `localStorage` is a MIRROR, never the record: it serves an auth-off dev stack and an offline tab,
 * and is written only after the server accepted a write.
 */

export type StoredLayoutRead<T> =
	| { readonly status: 'ok'; readonly layout: T }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

export interface StoredLayoutStore<T> {
	load(): Promise<StoredLayoutRead<T>>;
	save(layout: T): Promise<boolean>;
}

/** The document as the catalog serves it — mirrors `service_kit.schemas.dock_layout.DockLayouts`. */
interface DockLayoutsDocument<T> {
	workbenches: Record<string, T>;
}

interface Envelope<T> {
	exists?: boolean;
	value?: DockLayoutsDocument<T>;
}

export interface DockLayoutStoreOptions {
	/** Namespaces this workbench inside the ONE per-subject document, so zones cannot collide. */
	workbenchId: string;
	/**
	 * The zone's own path to the catalog's user-state document — e.g.
	 * `/lakehouse/capi/v1/user-state/dock-layout`. Zone-shaped by necessity: each zone reaches the
	 * catalog through its own base path and its own BFF proxy.
	 */
	endpoint: string;
	/** Injected for tests and for server-side reads (`getRequestEvent().fetch`). */
	fetcher?: typeof fetch;
	/** Mirror key prefix, so one browser holding two zones cannot cross-write. Defaults per endpoint. */
	mirrorKey?: string;
}

const readMirror = <T>(key: string): T | null => {
	if (typeof localStorage === 'undefined') return null;
	const raw = localStorage.getItem(key);
	if (raw === null) return null;
	try {
		return JSON.parse(raw) as T;
	} catch {
		return null;
	}
};

const writeMirror = (key: string, value: unknown): void => {
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(key, JSON.stringify(value));
	} catch {
		// A full or disabled store must never fail a save the SERVER already accepted.
	}
};

/** Shape guard: a stored `null` / `42` / `{}` parses fine but would make `workbenches` unindexable. */
const workbenchesOf = <T>(doc: DockLayoutsDocument<T> | undefined): Record<string, T> | null => {
	const w = doc?.workbenches;
	return typeof w === 'object' && w !== null ? w : null;
};

export function makeDockLayoutStore<T>(options: DockLayoutStoreOptions): StoredLayoutStore<T> {
	const { workbenchId, endpoint } = options;
	const fetcher = options.fetcher ?? fetch;
	const mirror = options.mirrorKey ?? `rask-dock-layout:${endpoint}`;

	/** The whole document, as one of the three outcomes. Shared by load and save. */
	async function readDocument(): Promise<StoredLayoutRead<Record<string, T>>> {
		let response: Response;
		try {
			response = await fetcher(endpoint);
		} catch (e) {
			// Unreachable is NOT absent. A tab that cannot reach the store has no idea whether the user
			// has saved work, and guessing "no" is the guess that destroys it.
			const mirrored = readMirror<DockLayoutsDocument<T>>(mirror);
			const w = workbenchesOf(mirrored ?? undefined);
			if (w !== null) return { status: 'ok', layout: w };
			return {
				status: 'unreadable',
				detail: e instanceof Error ? e.message : 'the layout store is unreachable',
			};
		}

		if (response.status === 409) {
			const detail = await response
				.json()
				.then((b: { detail?: string }) => b.detail ?? 'the stored layout cannot be read')
				.catch(() => 'the stored layout cannot be read');
			return { status: 'unreadable', detail };
		}
		if (response.status === 401) {
			// Signed out — an auth-off dev stack or an expired session. The mirror is the honest answer.
			const mirrored = readMirror<DockLayoutsDocument<T>>(mirror);
			const w = workbenchesOf(mirrored ?? undefined);
			return w === null ? { status: 'absent' } : { status: 'ok', layout: w };
		}
		if (!response.ok) {
			return { status: 'unreadable', detail: `the layout store answered ${response.status}` };
		}

		const body = (await response.json()) as Envelope<T>;
		if (body.exists !== true) return { status: 'absent' };
		const w = workbenchesOf(body.value);
		if (w === null) {
			return { status: 'unreadable', detail: 'the stored dock document has no workbenches map' };
		}
		writeMirror(mirror, body.value);
		return { status: 'ok', layout: w };
	}

	return {
		async load(): Promise<StoredLayoutRead<T>> {
			const doc = await readDocument();
			if (doc.status !== 'ok') return doc;
			const layout = doc.layout[workbenchId];
			// This workbench missing from an otherwise-fine document is genuinely absent — the user has
			// simply never arranged THIS one. Saving is safe.
			return layout === undefined ? { status: 'absent' } : { status: 'ok', layout };
		},

		async save(layout: T): Promise<boolean> {
			// Read-modify-write, not a blind PUT: ONE document holds every workbench this user has
			// arranged, so replacing it wholesale would delete the other zones' layouts. Autosave is
			// already debounced, which is what bounds the extra round trip.
			const doc = await readDocument();
			// Refuse rather than clobber — the second lock behind the caller's own refusal, catching a
			// document that became unreadable after a healthy start.
			if (doc.status === 'unreadable') return false;
			const existing = doc.status === 'ok' ? doc.layout : {};
			const next: DockLayoutsDocument<T> = { workbenches: { ...existing, [workbenchId]: layout } };

			try {
				const response = await fetcher(endpoint, {
					method: 'PUT',
					headers: { 'content-type': 'application/json' },
					body: JSON.stringify(next),
				});
				if (response.ok || response.status === 401) {
					// 401 = auth-off dev, where the mirror IS the store; reporting failure would break it.
					writeMirror(mirror, next);
					return true;
				}
				return false;
			} catch {
				return false;
			}
		},
	};
}
