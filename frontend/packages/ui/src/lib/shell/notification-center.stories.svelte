<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import NotificationCenter from './notification-center.svelte';
	import type { InboxNotificationLike } from './inbox.js';

	// The bell, in its shapes. It renders NOTHING on the server (the popover content is portalled), and
	// the estate has no other surface that shows the panel open.
	//
	// A pinned `now` keeps the relative times ("5m ago") stable, so a visual diff of this story is
	// about the component rather than about the clock.
	// No `component:` and no autodocs: each Story below supplies its own props.
	const { Story } = defineMeta({ title: 'Shell/NotificationCenter' });

	const NOW = Date.parse('2026-08-11T12:00:00Z');
	const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

	/** Inbox pointers — the TARGETED plane: rows that exist because they concern YOU. */
	const INBOX: InboxNotificationLike[] = [
		{
			notification_id: 'run-a@FAIL',
			reason: 'author',
			object_id: 'silver$pages',
			source_run_id: 'run-a',
			occurred_at: ago(5),
			seen: false,
		},
		{
			notification_id: 'run-d@COMPLETE',
			reason: 'project_watch',
			object_id: 'gold$daily',
			source_run_id: 'run-d',
			occurred_at: ago(30),
			seen: false,
		},
		{
			notification_id: 'run-e@COMPLETE',
			reason: 'author',
			object_id: 'bronze$pages',
			source_run_id: 'run-e',
			occurred_at: ago(180),
			seen: true,
		},
	];
</script>

<!-- WIRED: a zone that has an inbox transport. Badge counts the INBOX (2 unread). This is what every
     zone renders on a governed stack. -->
<Story name="With an inbox">
	<div class="flex justify-end p-8">
		<NotificationCenter inbox={INBOX} inboxUnread={2} now={NOW} />
	</div>
</Story>

<!-- UNWIRED: `inbox` is undefined — an auth-off dev stack, or a deployment with no notifications
     service. No badge, and the panel says there is no inbox. Kept as a story because it is a SHIPPED
     state, not a degraded one: `make dev-zone ZONE=home` renders exactly this. -->
<Story name="Without an inbox">
	<div class="flex justify-end p-8">
		<NotificationCenter now={NOW} />
	</div>
</Story>

<!-- The empty inbox is a DIFFERENT statement from an absent one: it means nothing has been addressed
     to you, which the panel says in words rather than showing a blank box. -->
<Story name="Empty inbox">
	<div class="flex justify-end p-8">
		<NotificationCenter inbox={[]} inboxUnread={0} now={NOW} />
	</div>
</Story>
