/**
 * The zone's client for per-subject user state — a user's own work, kept off their laptop.
 *
 * The workflow canvas and saved views lived in `localStorage`, so the same signed-in person opening the
 * estate on another machine found an empty canvas. The catalog now serves both documents at
 * `/v1/user-state/<document>`, keyed on the VERIFIED token subject (never a path or body parameter), and
 * this zone reaches them through its own scoped `capi/v1/user-state/[document]` proxy.
 *
 * Three outcomes, and keeping them distinct is the whole point:
 *
 *   - `ok`        — a document came back. Use it.
 *   - `absent`    — the user has genuinely never saved one. Seed a fresh one; saving is safe.
 *   - `unreadable`— a document EXISTS and could not be read (schema drift, or an owner mismatch), or the
 *                   store is unreachable. Do NOT seed and do NOT save. This is the case that loses work:
 *                   treat it as empty and the next autosave overwrites a record that is still there. The
 *                   server was fixed to answer 409 rather than `exists: false` for exactly this; the
 *                   client half is refusing to write over it.
 *
 * `localStorage` remains, but demoted to a MIRROR rather than the record: it is what an auth-off dev
 * stack and an offline tab read, and it is written after a successful server write so a reload is instant.
 * It is never the thing that decides whether the user has saved work.
 */

/** The documents this zone owns. Matches `UserStateDocument` in `services/common/user_state.py`. */
export type UserStateDocument = 'workflow-graph' | 'saved-views';

export type UserStateRead<T> =
	| { readonly status: 'ok'; readonly value: T }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

const endpoint = (document: UserStateDocument): string => `/media/capi/v1/user-state/${document}`;

/** The mirror key for a document — namespaced so it cannot collide with the zone's UI preferences. */
export const mirrorKey = (document: UserStateDocument): string => `lance-media-mirror:${document}`;

function readMirror<T>(document: UserStateDocument): T | null {
	if (typeof localStorage === 'undefined') return null;
	const raw = localStorage.getItem(mirrorKey(document));
	if (raw === null) return null;
	try {
		return JSON.parse(raw) as T;
	} catch {
		return null;
	}
}

function writeMirror(document: UserStateDocument, value: unknown): void {
	if (typeof localStorage === 'undefined') return;
	try {
		localStorage.setItem(mirrorKey(document), JSON.stringify(value));
	} catch {
		// A full or disabled store must never fail a save that the SERVER already accepted.
	}
}

/**
 * Read the caller's document.
 *
 * @param fetcher injected for tests; defaults to the ambient `fetch`.
 */
export async function readUserState<T>(
	document: UserStateDocument,
	fetcher: typeof fetch = fetch,
): Promise<UserStateRead<T>> {
	let response: Response;
	try {
		response = await fetcher(endpoint(document));
	} catch (e) {
		// Unreachable is NOT absent. A tab that cannot reach the store has no idea whether the user has
		// saved work, and guessing "no" is the guess that destroys it.
		const mirrored = readMirror<T>(document);
		if (mirrored !== null) return { status: 'ok', value: mirrored };
		return {
			status: 'unreadable',
			detail: e instanceof Error ? e.message : 'the store is unreachable',
		};
	}

	if (response.status === 409) {
		const detail = await response
			.json()
			.then((b: { detail?: string }) => b.detail ?? 'the stored document cannot be read')
			.catch(() => 'the stored document cannot be read');
		return { status: 'unreadable', detail };
	}
	if (response.status === 401) {
		// Signed out — an auth-off dev stack or an expired session. The mirror is the honest local answer.
		const mirrored = readMirror<T>(document);
		return mirrored === null ? { status: 'absent' } : { status: 'ok', value: mirrored };
	}
	if (!response.ok) {
		return { status: 'unreadable', detail: `the store answered ${response.status}` };
	}

	const body = (await response.json()) as { exists?: boolean; value?: T };
	if (body.exists !== true || body.value === undefined) return { status: 'absent' };
	writeMirror(document, body.value);
	return { status: 'ok', value: body.value };
}

/**
 * Save the caller's document. Returns whether the SERVER accepted it — a mirror write is not a save.
 *
 * Callers must not call this after a read returned `unreadable`: that is the overwrite this whole design
 * exists to prevent, and the server's 409 is the last line rather than the only one.
 */
export async function writeUserState(
	document: UserStateDocument,
	value: unknown,
	fetcher: typeof fetch = fetch,
): Promise<boolean> {
	try {
		const response = await fetcher(endpoint(document), {
			method: 'PUT',
			headers: { 'content-type': 'application/json' },
			body: JSON.stringify(value),
		});
		if (response.ok) {
			writeMirror(document, value);
			return true;
		}
		if (response.status === 401) {
			// Auth-off dev: the mirror IS the store, and reporting failure would break that stack.
			writeMirror(document, value);
			return true;
		}
		return false;
	} catch {
		return false;
	}
}
