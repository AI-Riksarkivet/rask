<script lang="ts">
	import { CircleCheck, CircleX, Inbox, X } from '@lucide/svelte';
	import { Badge } from '../components/badge/index.js';
	import { Button } from '../components/button/index.js';
	import { cn } from '../utils/cn.js';
	import { formatAbsolute, formatRelative } from '../utils/timestamp.js';
	import { orderedInbox, type InboxNotificationLike } from './inbox.js';

	// The INBOX rows — what was addressed to this subject, as opposed to the activity projection the
	// sibling list renders. Split out for the same reason `notification-list` was: the bell's popover
	// content is portalled and a portal renders nothing on the server, so markup that lives only
	// inside the centre cannot be asserted on its own.
	//
	// Pure presentation. It takes rows, never fetches, and reports dismissal UP so the owner of the
	// dismissed set stays the owner.
	let {
		rows,
		locallySeen = [],
		limit = 8,
		now = Date.now(),
		ondismiss,
		class: className,
	}: {
		/** Inbox rows as the service returns them; dismissed rows are already absent (the service
		 *  drops them from both filters), so there is nothing to pre-filter here. */
		rows: InboxNotificationLike[];
		/** Ids this TAB has marked read since the page loaded — the row's own `seen` is the server's
		 *  word, and a row marked in this tab must not flash back to unread before the write lands. */
		locallySeen?: string[];
		limit?: number;
		now?: number;
		ondismiss?: (notificationId: string) => void;
		class?: string;
	} = $props();

	const ordered = $derived(orderedInbox(rows));
	const shown = $derived(ordered.slice(0, limit));
	const readSet = $derived(new Set(locallySeen));

	// The state rides the id (`run_id@STATE`), which is the only phase information an inbox row
	// carries — it is a POINTER, not a copy of the event, so there is no status field to read.
	const stateOf = (row: InboxNotificationLike): string => {
		const at = row.notification_id.lastIndexOf('@');
		return at === -1 ? '' : row.notification_id.slice(at + 1).toUpperCase();
	};
	const failed = (row: InboxNotificationLike) => {
		const state = stateOf(row);
		return state === 'FAIL' || state === 'FAILED' || state === 'ABORT';
	};
	const label = (row: InboxNotificationLike) => {
		const state = stateOf(row);
		if (!state) return 'Update';
		return state.charAt(0) + state.slice(1).toLowerCase();
	};
	// A pointer names the object it concerns; the run id is the thing a reader can act on.
	const title = (row: InboxNotificationLike) =>
		row.object_id || row.source_run_id || row.notification_id;
</script>

{#if shown.length === 0}
	<div class="text-muted-foreground px-3 py-6 text-center text-sm">
		<Inbox class="mx-auto mb-2 size-5 opacity-60" aria-hidden="true" />
		<p>Nothing addressed to you yet.</p>
		<p class="mt-1 text-xs">
			Runs you start appear here when they finish — the Activity tab shows everything you can see.
		</p>
	</div>
{:else}
	<ul class={cn('divide-border/60 divide-y', className)} data-slot="inbox-list">
		{#each shown as row (row.notification_id)}
			{@const isRead = row.seen || readSet.has(row.notification_id)}
			{@const Icon = failed(row) ? CircleX : CircleCheck}
			<li
				class="flex items-start gap-2.5 px-3 py-2.5"
				data-slot="inbox-notification"
				data-unread={isRead ? undefined : ''}
			>
				<Icon
					class={cn('mt-0.5 size-4 shrink-0', failed(row) ? 'text-destructive' : 'text-success')}
					aria-hidden="true"
				/>
				<div class="min-w-0 flex-1">
					<div class="flex min-w-0 items-center gap-2">
						<!-- `leading-tight`, not `leading-none`: a 14px line box for a 14px font plus `truncate`'s
						     `overflow:hidden` slices every descender, which is how the run list rendered
						     "ingest_events" as "inqest_events" until a 4x zoom caught it. -->
						<p class="truncate text-sm leading-tight font-medium" title={title(row)}>
							{title(row)}
						</p>
						<Badge variant={failed(row) ? 'destructive' : 'success'} class="shrink-0">
							{label(row)}
						</Badge>
						{#if !isRead}
							<span class="bg-primary ml-auto size-1.5 shrink-0 rounded-full" aria-label="Unread"
							></span>
						{/if}
					</div>
					<p class="text-muted-foreground mt-1 truncate text-xs">
						<!-- Joined in the script rather than stitched from `{#if}` blocks: the compiler trims the
						     trailing space inside a block, which rendered "lance-medallion ·alice" once. -->
						{[row.reason, row.source_run_id].filter(Boolean).join(' · ')}
						{#if row.occurred_at}
							· <span title={formatAbsolute(row.occurred_at)}>{formatRelative(row.occurred_at, now)}</span>
						{/if}
					</p>
				</div>
				<Button
					variant="ghost"
					size="icon-xs"
					class="shrink-0"
					aria-label={`Dismiss ${title(row)}`}
					onclick={() => ondismiss?.(row.notification_id)}
				>
					<X aria-hidden="true" />
				</Button>
			</li>
		{/each}
	</ul>
	{#if ordered.length > shown.length}
		<p class="text-muted-foreground border-border/60 border-t px-3 py-2 text-center text-xs">
			{ordered.length - shown.length} older {ordered.length - shown.length === 1
				? 'notification'
				: 'notifications'} not shown
		</p>
	{/if}
{/if}
