<script lang="ts">
	import { Bell } from '@lucide/svelte';
	import { Button } from '../components/button/index.js';
	import * as Popover from '../components/popover/index.js';
	import InboxList from './inbox-list.svelte';
	import { cn } from '../utils/cn.js';
	import { inboxSeenOnClose, inboxUnreadCount, type InboxNotificationLike } from './inbox.js';

	// The estate's notification surface, in @rask/ui so every zone gets the SAME one — the header was
	// forked once already (the annotator hand-rolled its own) and drifted.
	//
	// It renders the subject's INBOX: rows addressed to this subject, durable per subject. It does NOT
	// fetch — the zone owns the transport (that is what keeps @rask/ui free of any BFF coupling and keeps
	// this testable), and hands the rows in as a prop.
	//
	// Read/dismiss state is bindable rather than owned: unbound it works standalone (per-tab memory),
	// bound it is the zone's to persist per subject. Both sets are keyed by NOTIFICATION id.
	//
	// `inbox === undefined` is the honest un-wired case — an auth-off dev stack or a zone whose
	// transport is absent — and there the bell has nothing to count and says so.
	let {
		inbox,
		inboxUnread,
		seen = $bindable([]),
		dismissed = $bindable([]),
		onseen,
		ondismiss,
		limit = 8,
		now = Date.now(),
		class: className,
	}: {
		/** Inbox rows — targeted, durable. `undefined` = this zone has no inbox transport, which is a
		 *  different statement from an EMPTY inbox and renders differently. */
		inbox?: InboxNotificationLike[];
		/** The server's unread count for the WHOLE inbox. Authoritative where present: the rows above
		 *  are one page, so deriving the badge from them would shrink it as a reader pages. */
		inboxUnread?: number;
		/** Notification ids already read. Bindable so a zone can persist them. */
		seen?: string[];
		/** Notification ids dismissed. Bindable so a zone can persist them. */
		dismissed?: string[];
		/** Called with the FULL seen set after the panel closes — the persistence seam. */
		onseen?: (seen: string[]) => void;
		/** Called with the dismissed notification id and the full dismissed set. */
		ondismiss?: (notificationId: string, dismissed: string[]) => void;
		/** Rows shown before the panel says how many more there are. */
		limit?: number;
		/** Injectable clock for the relative times. */
		now?: number;
		class?: string;
	} = $props();

	let open = $state(false);

	/** Whether this zone has an inbox at all. */
	const hasInbox = $derived(inbox !== undefined);
	const inboxRows = $derived(inbox ?? []);
	// Server count wins; the local derivation is the fallback for a page-only caller. `seen` carries
	// this tab's optimistic marks so the badge drops the moment the panel closes rather than waiting
	// on the write.
	const unreadCount = $derived(hasInbox ? (inboxUnread ?? inboxUnreadCount(inboxRows, seen)) : 0);
	// A count, not an inventory: past two digits the exact number stops being information.
	const badge = $derived(unreadCount > 99 ? '99+' : String(unreadCount));

	// Read on CLOSE, not on open: while the panel is open the new rows keep their unread mark, so the
	// viewer can see WHICH ones are new; the count clears once they have had the chance to look. Only
	// what was actually RENDERED is marked — a row pushed past the cap was never seen.
	function markSeen() {
		if (!hasInbox) return;
		const ids = inboxSeenOnClose(inboxRows, limit);
		const next = [...new Set([...seen, ...ids])];
		if (next.length === seen.length) return;
		seen = next;
		onseen?.(next);
	}

	function dismiss(id: string) {
		if (dismissed.includes(id)) return;
		const next = [...dismissed, id];
		dismissed = next;
		ondismiss?.(id, next);
	}

	function dismissAll() {
		const ids = inboxRows.map((row) => row.notification_id);
		if (ids.length === 0) return;
		const next = [...new Set([...dismissed, ...ids])];
		dismissed = next;
		for (const id of ids) ondismiss?.(id, next);
	}
</script>

<Popover.Root
	bind:open
	onOpenChange={(next) => {
		if (!next) markSeen();
	}}
>
	<Popover.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				size="icon"
				class={cn('relative rounded-full', className)}
				aria-label={unreadCount > 0 ? `Notifications, ${unreadCount} unread` : 'Notifications'}
			>
				<Bell class="size-4" aria-hidden="true" />
				{#if unreadCount > 0}
					<!-- The count lives on the trigger so it is legible with the panel CLOSED — that is the
					     whole point of the surface: nobody should have to hunt for a failed run. -->
					<span
						data-slot="notification-count"
						class="bg-destructive absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[0.625rem] leading-none font-semibold text-white tabular-nums"
					>
						{badge}
					</span>
				{/if}
			</Button>
		{/snippet}
	</Popover.Trigger>
	<!-- `role`/`aria-label` are ours, not the primitive's: bits-ui advertises `aria-haspopup="dialog"`
	     on the trigger but renders the content as a bare div with no role and no name (checked in the
	     browser — the open panel's attributes are class/tabindex/data-state/data-popover-content). So
	     a screen-reader user was told a dialog would open and then landed in an unnamed group. It
	     traps focus and closes on Escape, so `dialog` is the honest role for it. -->
	<Popover.Content
		class="w-96 max-w-[calc(100vw-2rem)] p-0"
		align="end"
		role="dialog"
		aria-label="Notifications"
	>
		<div class="border-border/60 flex items-center gap-2 border-b px-3 py-2">
			<p class="text-sm font-medium">Notifications</p>
			{#if unreadCount > 0}
				<span class="text-muted-foreground text-xs">{unreadCount} unread</span>
			{/if}
			<Button
				variant="ghost"
				size="xs"
				class="ml-auto"
				disabled={inboxRows.length === 0}
				onclick={dismissAll}
			>
				Dismiss all
			</Button>
		</div>
		<div class="max-h-[60svh] overflow-y-auto">
			{#if hasInbox}
				<InboxList rows={inboxRows} locallySeen={seen} {limit} {now} ondismiss={dismiss} />
			{:else}
				<p class="text-muted-foreground px-3 py-6 text-center text-sm" data-slot="inbox-unwired">
					No notification inbox on this stack.
				</p>
			{/if}
		</div>
	</Popover.Content>
</Popover.Root>
