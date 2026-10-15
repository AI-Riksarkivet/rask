import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import NotificationCenter from '../src/lib/shell/notification-center.svelte';
import TopNavbar from '../src/lib/shell/top-navbar.svelte';
import type { InboxNotificationLike } from '../src/lib/shell/inbox.js';

// What a user SEES, asserted on rendered markup rather than on props — a prop assertion passes on a
// component that renders nothing at all.
//
// These render on the SERVER (`svelte/server`), which is exactly how a zone first paints them. That
// puts one boundary on this file: the bell's panel is portalled, and a portal emits nothing during
// SSR, so only the BELL — count, label — is asserted here, by rendering `NotificationCenter`.

/** The visible text of a render — tags and Svelte's SSR comment markers stripped, whitespace
 *  collapsed. This is what a reader of the panel actually reads.
 *
 *  Tags are walked rather than regex-stripped, because a tailwind class list contains `>` (the
 *  badge's `[&>svg]:size-3`): a `<[^>]+>` strip ends the tag early and leaks half the stylesheet
 *  into the "text", which is how the first run of this file failed. */
const text = (html: string) => {
	let out = '';
	let inTag = false;
	let quote = '';
	for (const ch of html.replace(/<!--[\s\S]*?-->/g, '')) {
		if (!inTag) {
			if (ch === '<') {
				inTag = true;
				out += ' ';
			} else out += ch;
		} else if (quote) {
			if (ch === quote) quote = '';
		} else if (ch === '"' || ch === "'") quote = ch;
		else if (ch === '>') inTag = false;
	}
	return out
		.replace(/&amp;/g, '&')
		.replace(/&#39;/g, "'")
		.replace(/&quot;/g, '"')
		.replace(/\s+/g, ' ')
		.trim();
};

const NOW = Date.parse('2026-07-26T13:55:00+00:00');

const INBOX: InboxNotificationLike[] = [
	{ notification_id: 'run-a@FAIL', reason: 'author', object_id: 'silver$pages', seen: false },
	{ notification_id: 'run-b@COMPLETE', reason: 'author', object_id: 'gold$daily', seen: false },
	{ notification_id: 'run-c@COMPLETE', reason: 'author', object_id: 'gold$weekly', seen: true },
];

const centre = (props: Record<string, unknown> = {}) =>
	render(NotificationCenter, { props: { inbox: INBOX, now: NOW, ...props } }).body;

describe('the bell', () => {
	it('carries the unread count where it can be read with the panel shut', () => {
		const html = centre();
		expect(html).toContain('aria-label="Notifications, 2 unread"');
		expect(text(html)).toBe('2');
	});

	it("drops this tab's optimistic marks from the count", () => {
		const html = centre({ seen: ['run-a@FAIL', 'run-b@COMPLETE'] });
		expect(html).toContain('aria-label="Notifications"');
		expect(html).not.toContain('data-slot="notification-count"');
	});

	it("takes the server's unread count over the page it was handed", () => {
		expect(text(centre({ inboxUnread: 150 }))).toBe('99+');
		expect(centre({ inboxUnread: 150 })).toContain('aria-label="Notifications, 150 unread"');
	});

	it('counts nothing when the zone has no inbox transport', () => {
		expect(centre({ inbox: undefined })).not.toContain('data-slot="notification-count"');
	});

	it('is absent from the navbar until a zone hands the shell a feed', () => {
		// The opt-in half of the contract: no feed, no bell, and the account cluster keeps its position.
		const bare = render(TopNavbar, { props: { pathname: '/lakehouse' } }).body;
		expect(bare).not.toContain('Notifications');

		const wired = render(TopNavbar, {
			props: { pathname: '/lakehouse', notifications: { inbox: INBOX } },
		}).body;
		expect(wired).toContain('aria-label="Notifications, 2 unread"');
	});
});
