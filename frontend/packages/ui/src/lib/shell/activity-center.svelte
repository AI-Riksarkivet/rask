<script lang="ts" module>
	import type { ActivityEntryLike } from './nav-config.js';

	/** One day's entries, newest first, under the day's heading. */
	type DayGroup = { key: string; heading: string; entries: ActivityEntryLike[] };

	const dayKey = (at: number): string => new Date(at).toDateString();

	/** Entries (already newest first) folded into days, each headed "Today", "Yesterday" or its date. */
	function groupByDay(entries: readonly ActivityEntryLike[], now: number): DayGroup[] {
		const today = dayKey(now);
		const yesterday = dayKey(now - 24 * 60 * 60 * 1000);
		const groups: DayGroup[] = [];
		for (const entry of entries) {
			const key = dayKey(entry.at);
			let group = groups.at(-1);
			if (group?.key !== key) {
				const heading =
					key === today
						? 'Today'
						: key === yesterday
							? 'Yesterday'
							: new Date(entry.at).toLocaleDateString(undefined, {
									weekday: 'long',
									month: 'short',
									day: 'numeric',
								});
				group = { key, heading, entries: [] };
				groups.push(group);
			}
			group.entries.push(entry);
		}
		return groups;
	}

	/** "Failed (HTTP 409)", "Failed (unreachable)" or "Failed". */
	function failureLabel(status: number | null): string {
		if (status === null) return 'Failed';
		if (status === 0) return 'Failed (unreachable)';
		return `Failed (HTTP ${status})`;
	}
</script>

<script lang="ts">
	import { Bell, CircleCheck, CircleX } from '@lucide/svelte';
	import { Button } from '../components/button/index.js';
	import * as Popover from '../components/popover/index.js';
	import { cn } from '../utils/cn.js';
	import { formatZoned } from '../utils/timestamp.js';

	// The bell: the reader's OWN activity, the outcome of every write the console sent for them, kept
	// in their browser by the console. It does not fetch and does not store: the console hands the
	// entries in and hears back when the panel opens (every entry is then seen) and when the reader
	// clears the log.
	let {
		entries,
		unread,
		onopen,
		onclear,
		clock = Date.now,
		class: className,
	}: {
		/** Newest first. */
		entries: readonly ActivityEntryLike[];
		/** Entries recorded since the panel was last opened: the badge. */
		unread: number;
		onopen?: () => void;
		onclear?: () => void;
		/** Injectable clock for the day headings, read each time the panel opens. */
		clock?: () => number;
		class?: string;
	} = $props();

	// "Today" is the day the panel was opened on, read at the opening, so a tab left open past
	// midnight files yesterday's writes under Yesterday.
	let openedAt = $state(0);
	const groups = $derived(groupByDay(entries, openedAt));
	// A count, not an inventory: past two digits the exact number stops being information.
	const badge = $derived(unread > 99 ? '99+' : String(unread));
</script>

<Popover.Root
	onOpenChange={(open) => {
		if (!open) return;
		openedAt = clock();
		onopen?.();
	}}
>
	<Popover.Trigger>
		{#snippet child({ props })}
			<Button
				{...props}
				variant="ghost"
				size="icon"
				class={cn('relative rounded-full', className)}
				aria-label={unread > 0 ? `Activity, ${unread} new` : 'Activity'}
			>
				<Bell class="size-4" aria-hidden="true" />
				{#if unread > 0}
					<span
						data-slot="activity-count"
						class="bg-destructive absolute -top-0.5 -right-0.5 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[0.625rem] leading-none font-semibold text-white tabular-nums"
					>
						{badge}
					</span>
				{/if}
			</Button>
		{/snippet}
	</Popover.Trigger>
	<!-- bits-ui renders the content as a bare div with no role and no name, while the trigger
	     advertises `aria-haspopup="dialog"`; it traps focus and closes on Escape, so `dialog` is the
	     honest role for it. -->
	<Popover.Content
		class="w-96 max-w-[calc(100vw-2rem)] p-0"
		align="end"
		role="dialog"
		aria-label="Activity"
	>
		<div class="border-border/60 flex items-center gap-2 border-b px-3 py-2">
			<p class="text-sm font-medium">Activity</p>
			<span class="text-muted-foreground text-xs">Your writes in this tab</span>
			<Button
				variant="ghost"
				size="xs"
				class="ml-auto"
				disabled={entries.length === 0}
				onclick={() => onclear?.()}
			>
				Clear
			</Button>
		</div>
		<div class="max-h-[60svh] overflow-y-auto" data-slot="activity-list">
			{#if entries.length === 0}
				<p class="text-muted-foreground px-3 py-6 text-center text-sm">
					Nothing yet. What you change here is listed with its outcome.
				</p>
			{:else}
				{#each groups as group (group.key)}
					<section aria-label={group.heading}>
						<h3
							class="text-muted-foreground bg-muted/40 px-3 py-1 text-[0.6875rem] font-semibold tracking-wide uppercase"
						>
							{group.heading}
						</h3>
						<ul class="divide-border/60 divide-y">
							{#each group.entries as entry (entry.id)}
								<li class="flex items-start gap-2.5 px-3 py-2.5" data-slot="activity-entry">
									{#if entry.ok}
										<CircleCheck class="text-success mt-0.5 size-4 shrink-0" aria-hidden="true" />
									{:else}
										<CircleX class="text-destructive mt-0.5 size-4 shrink-0" aria-hidden="true" />
									{/if}
									<div class="min-w-0 flex-1">
										<p class="text-sm font-medium break-words">{entry.label}</p>
										<time
											class="text-muted-foreground block text-xs tabular-nums"
											datetime={new Date(entry.at).toISOString()}
										>
											{formatZoned(entry.at)}
										</time>
										{#if entry.ok}
											<p class="text-success text-xs">Succeeded</p>
										{:else}
											<p class="text-destructive text-xs">{failureLabel(entry.status)}</p>
											{#if entry.detail}
												<p class="text-muted-foreground mt-0.5 text-xs break-words">
													{entry.detail}
												</p>
											{/if}
										{/if}
									</div>
								</li>
							{/each}
						</ul>
					</section>
				{/each}
			{/if}
		</div>
	</Popover.Content>
</Popover.Root>
