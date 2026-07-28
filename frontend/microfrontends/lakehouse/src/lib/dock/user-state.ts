/**
 * This zone's client for per-subject user state, narrowed to the dock-layout document.
 *
 * Three outcomes, and keeping them distinct is the entire point:
 *
 *   - `ok`         — a document came back. Use it.
 *   - `absent`     — the user has genuinely never saved one. Seed a default; saving is safe.
 *   - `unreadable` — one EXISTS and could not be read (schema drift, an owner mismatch, or the store is
 *                    unreachable). Do NOT seed and do NOT save. This is the case that loses work: treat
 *                    it as empty and the next autosave overwrites a workspace that is still there. The
 *                    catalog answers 409 rather than `exists: false` for exactly this; refusing to write
 *                    over it is the client half.
 *
 * `localStorage` is a MIRROR, never the record. It is what an auth-off dev stack and an offline tab
 * read, and it is written only after the server accepted a write. It never decides whether the user has
 * saved work — that question belongs to the store.
 *
 * Modelled on the media zone's `$lib/user-state.ts`, deliberately not shared with it: the two zones have
 * different document sets and different base paths, and a premature shared module would have to know
 * both. If a third zone needs this, THAT is when it moves to `@rask/api`.
 */
import { base } from '$app/paths';

/** The documents this zone owns. Matches `UserStateDocument` in `service_kit/governed/user_state.py`. */
export type UserStateDocument = 'dock-layout';

export type UserStateRead<T> =
	| { readonly status: 'ok'; readonly value: T }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

const endpoint = (document: UserStateDocument): string => `${base}/capi/v1/user-state/${document}`;

/** The mirror key — namespaced so it cannot collide with this zone's UI preferences. */
export const mirrorKey = (document: UserStateDocument): string => `lakehouse-mirror:${document}`;

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
		// A full or disabled store must never fail a save the SERVER already accepted.
	}
}

/** Read the caller's document. `fetcher` is injected for tests; defaults to the ambient `fetch`. */
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
