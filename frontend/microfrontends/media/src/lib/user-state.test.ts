import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	mirrorKey,
	readUserState,
	writeUserState,
	type UserStateReader,
	type UserStateWriter,
} from '$lib/user-state';

/**
 * The three outcomes, and above all the difference between "you have nothing saved" and "we cannot read
 * what you have". Conflating them is what destroys a user's work: told the document is empty, the canvas
 * seeds a fresh one and the next autosave overwrites a record that is still there. The server was changed
 * to answer 409 instead of `exists: false` for that case; these assert the client half — that the tab
 * neither seeds nor writes over it.
 *
 * The transport is now the `readUserStateDoc`/`writeUserStateDoc` remote pair rather than a `fetch`, so
 * the stubs below hand back the `ApiResult` those functions return. Same statuses, same assertions — a
 * status is a status whether it arrived as a Response or as a discriminated union.
 */
const store = new Map<string, string>();

beforeEach(() => {
	store.clear();
	vi.stubGlobal('localStorage', {
		getItem: (k: string) => store.get(k) ?? null,
		setItem: (k: string, v: string) => void store.set(k, v),
		removeItem: (k: string) => void store.delete(k),
	});
});
afterEach(() => vi.unstubAllGlobals());

/** A store that answered with a document envelope. */
const answered =
	(data: { exists?: boolean; value?: unknown }): UserStateReader =>
	async () => ({ ok: true, data });
/** A store that answered with a status. `0` is UNREACHABLE — what the remote function returns when the
 *  catalog itself cannot be reached. */
const refused =
	(status: number, detail = ''): UserStateReader =>
	async () => ({ ok: false, status, detail });
const writeAnswered = (): UserStateWriter => async () => ({ ok: true, data: {} });
const writeRefused =
	(status: number): UserStateWriter =>
	async () => ({ ok: false, status, detail: '' });

describe('readUserState', () => {
	it('a saved document comes back, and is mirrored for an instant reload', async () => {
		const got = await readUserState<{ nodes: number[] }>(
			'workflow-graph',
			answered({ exists: true, value: { nodes: [1, 2] } }),
		);

		expect(got).toEqual({ status: 'ok', value: { nodes: [1, 2] } });
		expect(store.get(mirrorKey('workflow-graph'))).toBe('{"nodes":[1,2]}');
	});

	it('never saved is ABSENT — the caller may seed', async () => {
		expect(await readUserState('saved-views', answered({ exists: false }))).toEqual({
			status: 'absent',
		});
	});

	it('a 409 is UNREADABLE, never absent — this is the data-loss case', async () => {
		const got = await readUserState(
			'workflow-graph',
			refused(409, 'your saved workflow-graph exists but cannot be read'),
		);

		expect(got.status).toBe('unreadable');
		// The message has to reach the user: "we could not read it" and "you have nothing" look identical
		// on a blank canvas, and only one of them means their work is still there.
		expect(got).toHaveProperty('detail', expect.stringContaining('cannot be read'));
	});

	it('an unreachable store with no mirror is UNREADABLE, not absent', async () => {
		expect((await readUserState('workflow-graph', refused(0, 'network down'))).status).toBe(
			'unreadable',
		);
	});

	it('an unreachable ZONE SERVER (a rejected call) is unreadable too, never absent', async () => {
		// The second unreachable: an offline tab, where the remote call itself rejects rather than
		// reporting a status. It must not escape as an unhandled rejection and must not read as empty.
		const offline: UserStateReader = async () => {
			throw new Error('failed to fetch');
		};

		expect((await readUserState('workflow-graph', offline)).status).toBe('unreadable');
	});

	it('an unreachable store WITH a mirror serves the mirror rather than claiming emptiness', async () => {
		store.set(mirrorKey('saved-views'), JSON.stringify([{ name: 'mine' }]));

		expect(await readUserState('saved-views', refused(0, 'network down'))).toEqual({
			status: 'ok',
			value: [{ name: 'mine' }],
		});
	});

	it('a 500 is unreadable — any non-answer is, since none of them mean "empty"', async () => {
		expect((await readUserState('saved-views', refused(500))).status).toBe('unreadable');
	});
});

describe('writeUserState', () => {
	it('reports the SERVER outcome, and mirrors only what the server took', async () => {
		expect(await writeUserState('saved-views', [{ name: 'a' }], writeAnswered())).toBe(true);
		expect(store.get(mirrorKey('saved-views'))).toBe('[{"name":"a"}]');
	});

	it('a rejected write is false, and does NOT mirror — a mirror write is not a save', async () => {
		expect(await writeUserState('workflow-graph', { nodes: [] }, writeRefused(409))).toBe(false);
		expect(store.has(mirrorKey('workflow-graph'))).toBe(false);
	});

	it('signed out keeps the auth-off dev stack working through the mirror', async () => {
		expect(await writeUserState('saved-views', [{ name: 'dev' }], writeRefused(401))).toBe(true);
		expect(store.get(mirrorKey('saved-views'))).toBe('[{"name":"dev"}]');
	});
});
