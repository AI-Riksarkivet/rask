<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import NotificationCenter from './notification-center.svelte';
	import type { RunStatusLike } from '../runs/run-status.js';
	import type { InboxNotificationLike } from './inbox.js';

	// The bell, in both of its two shapes. It renders NOTHING on the server (the popover content is
	// portalled), and the estate has no other surface that shows the panel open — so before this story
	// the only way to look at the Inbox/Activity split was to run a whole auth-on zone against a live
	// notifications service. That is why it went unreviewed by eye for a slice.
	//
	// A pinned `now` keeps the relative times ("5m ago") stable, so a visual diff of this story is
	// about the component rather than about the clock.
	// No `component:` and no autodocs: with them, Storybook ALSO renders the component from empty
	// args, and `runs` is a required prop — `visibleRuns(undefined)` throws before any story paints.
	// Each Story below supplies its own props, which is the whole point of the two shapes.
	const { Story } = defineMeta({ title: 'Shell/NotificationCenter' });

	const NOW = Date.parse('2026-08-11T12:00:00Z');
	const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

	/** `GET /runs` rows — the ACTIVITY plane: every run whose outputs you may read, whoever ran it. */
	const RUNS: RunStatusLike[] = [
		{
			run_id: 'run-a',
			job: 'lance-medallion.ingest_events',
			state: 'FAILED',
			author: 'data_eng',
			started_at: ago(9),
			updated_at: ago(5),
			error_message: 'silver$pages: schema drift — column `region` absent in fragment 41',
			outputs: ['silver$pages'],
		},
		{
			run_id: 'run-b',
			job: 'lance-medallion.aggregate_gold',
			state: 'COMPLETED',
			author: 'ray',
			started_at: ago(40),
			updated_at: ago(22),
			outputs: ['gold$daily'],
		},
		{
			run_id: 'run-c',
			job: 'lance-ray.transcribe',
			state: 'RUNNING',
			author: 'htr',
			started_at: ago(3),
			updated_at: ago(1),
			progress_done: 340,
			progress_total: 1200,
		},
	];

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

<!-- WIRED: a zone that has an inbox transport. Badge counts the INBOX (2 unread), and the panel
     splits Inbox / Activity. This is what every zone renders on a governed stack. -->
<Story name="With an inbox">
	<div class="flex justify-end p-8">
		<NotificationCenter
			runs={RUNS}
			inbox={INBOX}
			inboxUnread={2}
			now={NOW}
			allHref="/lakehouse/lineage/runs"
		/>
	</div>
</Story>

<!-- UNWIRED: `inbox` is undefined — an auth-off dev stack, or a deployment with no notifications
     service. No tabs, and the badge falls back to the run feed. Kept as a story because it is a
     SHIPPED state, not a degraded one: `make dev-zone ZONE=home` renders exactly this. -->
<Story name="Without an inbox (fallback)">
	<div class="flex justify-end p-8">
		<NotificationCenter runs={RUNS} now={NOW} allHref="/lakehouse/lineage/runs" />
	</div>
</Story>

<!-- The empty inbox is a DIFFERENT statement from an absent one: it means nothing has been addressed
     to you, which the panel says in words rather than showing a blank box. -->
<Story name="Empty inbox">
	<div class="flex justify-end p-8">
		<NotificationCenter runs={RUNS} inbox={[]} inboxUnread={0} now={NOW} />
	</div>
</Story>
