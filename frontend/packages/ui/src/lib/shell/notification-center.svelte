<script lang="ts">
	import { Bell } from '@lucide/svelte';
	import { Button } from '../components/button/index.js';
	import * as Popover from '../components/popover/index.js';
	import NotificationList from './notification-list.svelte';
	import InboxList from './inbox-list.svelte';
	import * as Tabs from '../components/tabs/index.js';
	import { cn } from '../utils/cn.js';
	import {
		runNotificationId,
		unreadRuns,
		visibleRuns,
		type RunStatusLike,
		seenOnClose,
	} from '../runs/run-status.js';
	import { inboxSeenOnClose, inboxUnreadCount, type InboxNotificationLike } from './inbox.js';

	// The estate's notification surface, in @rask/ui so every zone gets the SAME one — the header was
	// forked once already (the annotator hand-rolled its own) and drifted, so the run feed ships as a
	// shared component with a shared shape from day one.
	//
	// It renders run lifecycle from the lineage service's `GET /runs`: a run that STARTED, one that
	// COMPLETED, one that FAILED with its error_message, and progress where `progress_total` is set.
	// It does NOT fetch — the zone owns the transport (that is what keeps @rask/ui free of any BFF
	// coupling and keeps this testable), and hands the rows in as a prop.
	//
	// Read/dismiss state is bindable rather than owned: unbound it works standalone (per-tab memory),
	// bound it is the zone's to persist per subject. Both sets are keyed by NOTIFICATION id
	// (`run_id@STATE`), not by run — so dismissing "ingest_events started" still lets "ingest_events
	// failed" reach the viewer.
	//
	// S3 SPLIT THE SURFACE IN TWO, and the badge moved with it. The panel used to render `GET /runs`
	// alone — dataset-governed, so the count was *everyone's* work, and the read state a zone
	// persisted spoke for rows the inbox had never heard of. Now: an **Inbox** tab (targeted rows,
	// durable per subject) and an **Activity** tab (the projection, unchanged), with the badge
	// counting the inbox ALONE. `inbox === undefined` is the honest un-wired case — an auth-off dev
	// stack or a zone whose transport is absent — and there the badge falls back to the run feed and
	// the panel renders a single list, exactly as before, so nothing regresses on a stack that has no
	// notification service to talk to.
	let {
		runs,
		inbox,
		inboxUnread,
		seen = $bindable([]),
		dismissed = $bindable([]),
		onseen,
		ondismiss,
		allHref,
		limit = 8,
		now = Date.now(),
		class: className,
	}: {
		/** The run rows, as `GET /runs` returns them. */
		runs: RunStatusLike[];
		/** Inbox rows — targeted, durable. `undefined` = this zone has no inbox transport, which is a
		 *  different statement from an EMPTY inbox and renders differently (no tabs, run-feed badge). */
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
		/** Optional "see everything" destination (a zone's runs page); omitted → no footer link. */
		allHref?: string;
		/** Rows shown before the panel says how many more there are. */
		limit?: number;
		/** Injectable clock for the relative times. */
		now?: number;
		class?: string;
	} = $props();

	let open = $state(false);

	const visible = $derived(visibleRuns(runs, dismissed));
	const unread = $derived(unreadRuns(runs, seen, dismissed));

	/** Whether this zone has an inbox at all. Drives the tabs AND the badge, so the two can never
	 *  disagree about which plane is being counted. */
	const hasInbox = $derived(inbox !== undefined);
	const inboxRows = $derived(inbox ?? []);
	// Server count wins; the local derivation is the fallback for a page-only caller. `seen` carries
	// this tab's optimistic marks so the badge drops the moment the panel closes rather than waiting
	// on the write.
	const unreadCount = $derived(
		hasInbox ? (inboxUnread ?? inboxUnreadCount(inboxRows, seen)) : unread.length,
	);
	// A count, not an inventory: past two digits the exact number stops being information.
	const badge = $derived(unreadCount > 99 ? '99+' : String(unreadCount));

	// Read on CLOSE, not on open: while the panel is open the new rows keep their unread mark, so the
	// viewer can see WHICH ones are new; the count clears once they have had the chance to look.
	function markSeen() {
		// Only what was actually RENDERED. `visible` is the full ordered set; the list renders
		// `slice(0, limit)` and says "N older runs not shown". Marking the unsliced list meant opening and
		// closing the bell marked all 13 read — including a FAILED run pushed past the cap that the user
		// never saw — and `seen` is what a zone persists per subject, so the failure was gone for good.
		// That is the exact outcome this component exists to prevent.
		//
		// With an inbox wired, the ids that matter are the INBOX's: those are the rows the service can
		// actually store a read mark against. Marking run ids there wrote into a mailbox that held no
		// pointer for them — the S1 gap this slice closes.
		const ids = hasInbox ? inboxSeenOnClose(inboxRows, limit) : seenOnClose(visible, limit);
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
		// Whatever plane is on screen. With an inbox wired the visible rows ARE the inbox rows, and
		// dismissing run ids instead would name pointers the service does not hold — the same
		// wrong-plane mistake `markSeen` used to make.
		const ids = hasInbox
			? inboxRows.map((row) => row.notification_id)
			: visible.map(runNotificationId);
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
				disabled={hasInbox ? inboxRows.length === 0 : visible.length === 0}
				onclick={dismissAll}
			>
				Dismiss all
			</Button>
		</div>
		{#if hasInbox}
			<!-- Two planes, named. Inbox = addressed to you, durable per subject. Activity = everything
			     you may see, per tab. Conflating them is what made the badge count other people's work
			     and made a read mark evaporate on reload. -->
			<Tabs.Root value="inbox">
				<Tabs.List class="w-full rounded-none border-b px-3">
					<Tabs.Trigger value="inbox" data-slot="inbox-tab">
						Inbox
						{#if unreadCount > 0}
							<span class="text-muted-foreground ml-1.5 text-xs tabular-nums">{unreadCount}</span>
						{/if}
					</Tabs.Trigger>
					<Tabs.Trigger value="activity" data-slot="activity-tab">Activity</Tabs.Trigger>
				</Tabs.List>
				<Tabs.Content value="inbox" class="mt-0 max-h-[60svh] overflow-y-auto">
					<InboxList rows={inboxRows} locallySeen={seen} {limit} {now} ondismiss={dismiss} />
				</Tabs.Content>
				<Tabs.Content value="activity" class="mt-0 max-h-[60svh] overflow-y-auto">
					<NotificationList {runs} {seen} {dismissed} {limit} {now} ondismiss={dismiss} />
				</Tabs.Content>
			</Tabs.Root>
		{:else}
			<div class="max-h-[60svh] overflow-y-auto">
				<NotificationList {runs} {seen} {dismissed} {limit} {now} ondismiss={dismiss} />
			</div>
		{/if}
		{#if allHref}
			<div class="border-border/60 border-t px-3 py-2 text-center">
				<a
					href={allHref}
					data-sveltekit-reload
					class="text-muted-foreground hover:text-foreground text-xs underline-offset-4 hover:underline"
				>
					View all runs
				</a>
			</div>
		{/if}
	</Popover.Content>
</Popover.Root>
