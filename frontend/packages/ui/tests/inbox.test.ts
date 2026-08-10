import { describe, expect, it } from 'vitest';
import {
	inboxSeenOnClose,
	inboxUnreadCount,
	orderedInbox,
	type InboxNotificationLike,
} from '../src/lib/shell/inbox.js';

/**
 * The inbox plane's model — the half of S3 that is pure data.
 *
 * The component half (badge = inbox, the Inbox/Activity split) is asserted in
 * `notifications.test.ts` and, because the panel is PORTALLED and a portal renders nothing on the
 * server, ultimately in a browser. These are the rules a portal cannot hide.
 */

const row = (id: string, occurred_at: string, seen = false): InboxNotificationLike => ({
	notification_id: id,
	occurred_at,
	seen,
});

describe('orderedInbox', () => {
	it('is newest first', () => {
		const rows = [
			row('a@COMPLETE', '2026-08-01T00:00:00Z'),
			row('b@FAIL', '2026-08-03T00:00:00Z'),
			row('c@COMPLETE', '2026-08-02T00:00:00Z'),
		];
		expect(orderedInbox(rows).map((r) => r.notification_id)).toEqual([
			'b@FAIL',
			'c@COMPLETE',
			'a@COMPLETE',
		]);
	});

	it('breaks ties by id so the list cannot shuffle between renders', () => {
		// Two rows stamped in the same second is the ordinary case for a cascade, not an edge one.
		const same = '2026-08-01T00:00:00Z';
		const rows = [row('b@FAIL', same), row('a@COMPLETE', same)];
		const once = orderedInbox(rows).map((r) => r.notification_id);
		const twice = orderedInbox([...rows].reverse()).map((r) => r.notification_id);
		expect(once).toEqual(['a@COMPLETE', 'b@FAIL']);
		expect(twice).toEqual(once);
	});

	it('does not mutate its input', () => {
		const rows = [row('a@COMPLETE', '2026-08-01T00:00:00Z'), row('b@FAIL', '2026-08-03T00:00:00Z')];
		const before = rows.map((r) => r.notification_id);
		orderedInbox(rows);
		expect(rows.map((r) => r.notification_id)).toEqual(before);
	});

	it('sorts a row with no timestamp last rather than dropping it', () => {
		const rows = [{ notification_id: 'x@FAIL' }, row('a@COMPLETE', '2026-08-01T00:00:00Z')];
		expect(orderedInbox(rows).map((r) => r.notification_id)).toEqual(['a@COMPLETE', 'x@FAIL']);
	});
});

describe('inboxSeenOnClose', () => {
	it('marks only what was RENDERED, never the whole list', () => {
		// The rule the run feed learned the hard way: marking past the cap read a FAILED row the user
		// never saw — and this set is what a zone PERSISTS, so that failure was gone for good.
		const rows = Array.from({ length: 12 }, (_, i) =>
			row(
				`r${String(i).padStart(2, '0')}@COMPLETE`,
				`2026-08-${String(12 - i).padStart(2, '0')}T00:00:00Z`,
			),
		);
		expect(inboxSeenOnClose(rows, 3)).toHaveLength(3);
	});

	it('does not re-mark rows the server already has as seen', () => {
		const rows = [
			row('a@FAIL', '2026-08-03T00:00:00Z', true),
			row('b@COMPLETE', '2026-08-02T00:00:00Z'),
		];
		expect(inboxSeenOnClose(rows, 8)).toEqual(['b@COMPLETE']);
	});

	it('is empty for an empty inbox, so a close sends nothing', () => {
		expect(inboxSeenOnClose([], 8)).toEqual([]);
	});
});

describe('inboxUnreadCount', () => {
	it('counts unseen rows', () => {
		const rows = [
			row('a@FAIL', '2026-08-03T00:00:00Z'),
			row('b@COMPLETE', '2026-08-02T00:00:00Z', true),
			row('c@COMPLETE', '2026-08-01T00:00:00Z'),
		];
		expect(inboxUnreadCount(rows)).toBe(2);
	});

	it("subtracts this tab's optimistic marks, so the badge drops without waiting on the write", () => {
		const rows = [row('a@FAIL', '2026-08-03T00:00:00Z'), row('b@COMPLETE', '2026-08-02T00:00:00Z')];
		expect(inboxUnreadCount(rows, ['a@FAIL'])).toBe(1);
	});

	it('is zero for an empty inbox', () => {
		expect(inboxUnreadCount([])).toBe(0);
	});
});
