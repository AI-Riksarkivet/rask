/**
 * The inbox transport's contract with `services/notifications`.
 *
 * Pinned here rather than in a zone because the wire shape is the thing two planes have to agree
 * on, and the estate has already paid for the alternative once: the bell's `author` field was
 * dropped from a trim, every row rendered "unknown author", and it was caught by looking at a
 * screenshot. A field the surface reads is not an optional field, and a schema is where that is
 * cheap to assert.
 */

import { describe, expect, it } from 'vitest';

import {
	dismissNotification,
	inboxReadState,
	markInboxSeen,
	readInbox,
	type InboxFeed,
	type InboxNotification,
} from './inbox.js';

const json = (body: unknown, status = 200) =>
	new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

/** A fetch that records what it was asked for and answers `body`. */
function recorder(body: unknown, status = 200) {
	const calls: { url: string; init: RequestInit | undefined }[] = [];
	const fetchImpl = (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
		calls.push({ url: String(input), init });
		return Promise.resolve(json(body, status));
	};
	return { calls, fetch: fetchImpl as unknown as typeof globalThis.fetch };
}

const NOTICE = {
	notification_id: 'run-1@FAIL',
	reason: 'author',
	object_id: 'silver.pages',
	source_run_id: 'ingest-42',
	event_seq: 9001,
	occurred_at: '2026-08-09T10:00:00Z',
	seen: false,
	dismissed: false,
};

const req = (fetchImpl: typeof globalThis.fetch, bearer?: string) => ({
	fetch: fetchImpl,
	base: 'http://gw:8888/api/notifications',
	bearer,
});

describe('readInbox', () => {
	it('hangs /inbox off the base it was given, so the gateway row is the whole address', async () => {
		const rec = recorder({ notifications: [NOTICE], next_cursor: null, unread: 1 });

		await readInbox(req(rec.fetch));

		expect(rec.calls[0]?.url).toBe('http://gw:8888/api/notifications/inbox');
	});

	it('sends only the query params the caller actually set', async () => {
		// The service owns the default filter and the default page size (a settings field, so
		// measuring it later is a config change). A client that always spelled them out would pin
		// today's defaults into seven zones.
		const rec = recorder({ notifications: [], next_cursor: null, unread: 0 });

		await readInbox(req(rec.fetch), { state: 'all', limit: 100 });

		expect(rec.calls[0]?.url).toBe('http://gw:8888/api/notifications/inbox?state=all&limit=100');
	});

	it('forwards the session bearer — the inbox is addressed by the token sub and nothing else', async () => {
		const rec = recorder({ notifications: [], next_cursor: null, unread: 0 });

		await readInbox(req(rec.fetch, 'tok'));

		expect(new Headers(rec.calls[0]?.init?.headers).get('authorization')).toBe('Bearer tok');
	});

	it('parses a page, defaulting the two nullable pointer fields when the producer omits them', async () => {
		const rec = recorder({
			notifications: [{ ...NOTICE, source_run_id: undefined, event_seq: undefined }],
			next_cursor: 'b3B32',
			unread: 3,
		});

		const result = await readInbox(req(rec.fetch));

		expect(result).toEqual({
			ok: true,
			data: {
				notifications: [{ ...NOTICE, source_run_id: null, event_seq: null }],
				next_cursor: 'b3B32',
				unread: 3,
			},
		});
	});

	it('reads an unknown `reason` rather than refusing the page', async () => {
		// v2 (project watch) and v3 (governance) add members to a StrEnum that has exactly one today.
		// A closed picklist here would make a zone that has not shipped yet reject the whole feed
		// over a field it does not render.
		const rec = recorder({
			notifications: [{ ...NOTICE, reason: 'watch' }],
			next_cursor: null,
			unread: 1,
		});

		const result = await readInbox(req(rec.fetch));

		expect(result.ok).toBe(true);
	});

	it('is 502 CONTRACT DRIFT when a field the surface reads goes missing, not a silent undefined', async () => {
		const { notification_id: _dropped, ...withoutId } = NOTICE;
		const rec = recorder({ notifications: [withoutId], next_cursor: null, unread: 1 });

		const result = await readInbox(req(rec.fetch));

		expect(result.ok).toBe(false);
		if (!result.ok) {
			expect(result.status).toBe(502);
			expect(result.detail).toContain('notifications contract drift');
		}
	});

	it('is status 0 — UNREACHABLE — when no inbox is listening, and never throws', async () => {
		// The auth-off dev case this whole seam has to survive: no service, no session, the bell
		// still renders. A rejection crossing the remote boundary would skip the caller's fallback.
		const result = await readInbox(req(() => Promise.reject(new Error('ECONNREFUSED'))));

		expect(result).toEqual({ ok: false, status: 0, detail: 'Error: ECONNREFUSED' });
	});

	it('names the upstream on a refusal, so 401 vs 403 lands on the right service', async () => {
		const rec = recorder({}, 403);

		const result = await readInbox(req(rec.fetch));

		expect(result).toEqual({ ok: false, status: 403, detail: 'notifications answered 403' });
	});
});

describe('markInboxSeen', () => {
	it('POSTs the full seen set under the field name the service validates', async () => {
		const rec = recorder({ updated: 2, unread: 0 });

		const result = await markInboxSeen(req(rec.fetch), ['run-1@FAIL', 'run-2@COMPLETE']);

		expect(rec.calls[0]?.url).toBe('http://gw:8888/api/notifications/inbox/seen');
		expect(rec.calls[0]?.init?.method).toBe('POST');
		expect(rec.calls[0]?.init?.body).toBe(
			JSON.stringify({ notification_ids: ['run-1@FAIL', 'run-2@COMPLETE'] }),
		);
		expect(result).toEqual({ ok: true, data: { updated: 2, unread: 0 } });
	});

	it('sends the empty set as an empty set — the panel legitimately renders nothing', async () => {
		const rec = recorder({ updated: 0, unread: 0 });

		await markInboxSeen(req(rec.fetch), []);

		expect(rec.calls[0]?.init?.body).toBe(JSON.stringify({ notification_ids: [] }));
	});
});

describe('dismissNotification', () => {
	it('POSTs ONE id, keyed by run_id@STATE', async () => {
		// Keyed by state on purpose: dismissing a run's "started" must leave its later "failed" free
		// to arrive, which a backend keyed on the run alone would silence.
		const rec = recorder({ dismissed: 1, unread: 0 });

		const result = await dismissNotification(req(rec.fetch), 'run-1@START');

		expect(rec.calls[0]?.url).toBe('http://gw:8888/api/notifications/inbox/dismiss');
		expect(rec.calls[0]?.init?.body).toBe(JSON.stringify({ notification_id: 'run-1@START' }));
		expect(result).toEqual({ ok: true, data: { dismissed: 1, unread: 0 } });
	});
});

describe('inboxReadState', () => {
	const feed = (notifications: InboxNotification[]): InboxFeed => ({
		notifications,
		next_cursor: null,
		unread: notifications.filter((n) => !n.seen && !n.dismissed).length,
	});

	it('lifts the read ids out of a page, and only the read ones', () => {
		const state = inboxReadState(
			feed([
				{ ...NOTICE, notification_id: 'a@FAIL', seen: true },
				{ ...NOTICE, notification_id: 'b@COMPLETE', seen: false },
			]),
		);

		expect(state.seen).toEqual(['a@FAIL']);
	});

	it('returns an EMPTY dismissed set from a live page — the wire cannot report one', () => {
		// Not an oversight and not a stub: the service drops dismissed rows from both filters, so a
		// dismissed notification is ABSENT from the page rather than present with the flag set. S1
		// therefore persists dismissal (the inbox count is right) without being able to re-seed the
		// bell's dismissed set, and S3 removes the question by rendering inbox rows.
		const state = inboxReadState(
			feed([{ ...NOTICE, notification_id: 'a@FAIL', seen: true, dismissed: false }]),
		);

		expect(state.dismissed).toEqual([]);
	});

	it('answers empty sets for an inbox nobody has written to', () => {
		expect(inboxReadState(feed([]))).toEqual({ seen: [], dismissed: [] });
	});
});
