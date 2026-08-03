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
 *
 * The TRANSPORT is injected (`$lib/catalog/remote/catalog.remote`'s `readUserStateDoc` /
 * `writeUserStateDoc`, bound by the caller) rather than imported here. That keeps this module — the one
 * that decides whether a user's work is safe to overwrite — a pure function of what the store answered,
 * testable without a server, exactly as it was when the answer came from a `fetch`.
 */
import type { ApiResult } from '@rask/api/client';

/** The documents this zone owns. Matches `UserStateDocument` in `services/common/user_state.py`. */
export type UserStateDocument = 'workflow-graph' | 'saved-views' | 'dock-layout';

/** What the catalog returns for a document: `exists: false` is a genuine "never saved", and a value is
 *  only a value when both keys agree. */
export interface UserStateEnvelope {
	exists?: boolean;
	value?: unknown;
}

export type UserStateRead<T> =
	| { readonly status: 'ok'; readonly value: T }
	| { readonly status: 'absent' }
	| { readonly status: 'unreadable'; readonly detail: string };

/** The read half of the transport: the caller's document, or the status the store answered with. */
export type UserStateReader = (args: {
	document: UserStateDocument;
}) => Promise<ApiResult<UserStateEnvelope>>;

/** The write half. Only `ok` counts as a save. */
export type UserStateWriter = (args: {
	document: UserStateDocument;
	value: unknown;
}) => Promise<ApiResult<unknown>>;

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
 * @param read the transport — `readUserStateDoc` in the app, a stub in tests.
 */
export async function readUserState<T>(
	document: UserStateDocument,
	read: UserStateReader,
): Promise<UserStateRead<T>> {
	// Two distinct unreachables, both of which mean "we do not know what is stored": the CATALOG is down
	// (the transport answers status 0) and the ZONE SERVER is unreachable — an offline tab, where the
	// remote call itself rejects. The mirror exists for exactly the second one, so it must still be
	// caught here rather than escaping as an unhandled rejection into the saved-views effect.
	let result: ApiResult<UserStateEnvelope>;
	try {
		result = await read({ document });
	} catch (e) {
		result = {
			ok: false,
			status: 0,
			detail: e instanceof Error ? e.message : 'the store is unreachable',
		};
	}

	if (result.ok) {
		const body = result.data;
		if (body.exists !== true || body.value === undefined) return { status: 'absent' };
		writeMirror(document, body.value);
		return { status: 'ok', value: body.value as T };
	}

	if (result.status === 0) {
		// Unreachable is NOT absent. A tab that cannot reach the store has no idea whether the user has
		// saved work, and guessing "no" is the guess that destroys it.
		const mirrored = readMirror<T>(document);
		if (mirrored !== null) return { status: 'ok', value: mirrored };
		return { status: 'unreadable', detail: result.detail || 'the store is unreachable' };
	}
	if (result.status === 409) {
		return { status: 'unreadable', detail: result.detail || 'the stored document cannot be read' };
	}
	if (result.status === 401) {
		// Signed out — an auth-off dev stack or an expired session. The mirror is the honest local answer.
		const mirrored = readMirror<T>(document);
		return mirrored === null ? { status: 'absent' } : { status: 'ok', value: mirrored };
	}
	return { status: 'unreadable', detail: `the store answered ${result.status}` };
}

/**
 * Save the caller's document. Returns whether the SERVER accepted it — a mirror write is not a save.
 *
 * Callers must not call this after a read returned `unreadable`: that is the overwrite this whole design
 * exists to prevent, and the server's 409 is the last line rather than the only one.
 *
 * @param write the transport — `writeUserStateDoc` in the app, a stub in tests.
 */
export async function writeUserState(
	document: UserStateDocument,
	value: unknown,
	write: UserStateWriter,
): Promise<boolean> {
	let result: ApiResult<unknown>;
	try {
		result = await write({ document, value });
	} catch {
		// An offline tab: the write never reached the server, so it is not a save.
		return false;
	}
	if (result.ok) {
		writeMirror(document, value);
		return true;
	}
	if (result.status === 401) {
		// Auth-off dev: the mirror IS the store, and reporting failure would break that stack.
		writeMirror(document, value);
		return true;
	}
	return false;
}
