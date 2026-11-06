// Shared by the mock inbox (`e2e/mock-notifications.ts`) and the specs that read it; a module of its
// own because importing the mock would start its server inside the test runner.

/** The inbox every signed-in person reads: two unread rows and one already seen, in the wire shape
 *  `@rask/api/inbox`'s `InboxFeedSchema` parses. `unread` is the WHOLE inbox's count, which the bell
 *  shows as its badge. */
export const MOCK_INBOX = {
	notifications: [
		{ notification_id: 'run-a@FAIL', seen: false },
		{ notification_id: 'run-b@COMPLETE', seen: false },
		{ notification_id: 'run-c@COMPLETE', seen: true },
	].map(({ notification_id, seen }) => ({
		notification_id,
		reason: 'author',
		object_id: 'acme.bronze.pages',
		source_run_id: notification_id.split('@')[0],
		event_seq: null,
		occurred_at: '2026-11-06T08:00:00Z',
		seen,
		dismissed: false,
	})),
	next_cursor: null,
	unread: 2,
};
