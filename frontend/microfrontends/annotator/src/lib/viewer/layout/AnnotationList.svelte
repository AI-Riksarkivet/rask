<script lang="ts">
	// Searchable annotation list — the review queue. Predictions first, highest
	// uncertainty first (the active-learning order), then click to select on canvas.
	// The filter is @rask/ui's DataTableTextFilter — the same search-icon + input pairing the
	// data/admin list toolbars use (and the only route to @rask/ui's own Input, which the
	// package does not export directly).
	import { DataTableTextFilter } from '@rask/ui/data-table';
	import { cn } from '@rask/ui/utils';
	import { statusDot } from './statusStyle';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	let filter = $state('');

	// m:ss for a segment's time range (audio/video rows); null when it has no time span.
	function timeRange(tStart: number | null, tEnd: number | null): string | null {
		if (tStart == null || tEnd == null || tEnd <= tStart) return null;
		const at = (s: number) =>
			`${Math.floor(s / 60)}:${Math.floor(s % 60)
				.toString()
				.padStart(2, '0')}`;
		return `${at(tStart)}–${at(tEnd)}`;
	}

	// The review order lives on the controller (shared with accept-and-advance); the
	// list only adds its text filter on top.
	const queue = $derived(
		controller.reviewQueue.filter((r) => {
			const q = filter.trim().toLowerCase();
			if (!q) return true;
			return (r.text + ' ' + r.label + ' ' + r.group).toLowerCase().includes(q);
		}),
	);
</script>

<div class="flex min-h-0 flex-1 flex-col" data-testid="annotation-list">
	<div class="px-3 py-2">
		<DataTableTextFilter bind:value={filter} placeholder="Filter annotations…" class="max-w-none" />
	</div>

	<ul class="min-h-0 flex-1 overflow-y-auto px-1.5 pb-2">
		{#each queue as r (r.index)}
			{@const range = timeRange(r.tStart, r.tEnd)}
			<li>
				<button
					class={cn(
						'hover:bg-muted/60 flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors',
						(controller.selectedIndex === r.index || controller.selectedSet.has(r.index)) &&
							'bg-primary/10 ring-primary/40 ring-1',
					)}
					onclick={(e) =>
						e.shiftKey || e.metaKey || e.ctrlKey
							? controller.toggleSelect(r.index)
							: controller.select(r.index)}
				>
					<span class={cn('mt-1 size-2 shrink-0 rounded-full', statusDot(r.status))}></span>
					<!-- One primary line at the estate's text-sm, meta beneath at text-xs — the same
					     two-step scale the data/admin lists use, replacing this zone's old
					     text-[10px]/text-[11px] one-offs. -->
					<span class="min-w-0 flex-1">
						<span class="flex items-center justify-between gap-2">
							<span class="truncate text-sm font-medium">{r.label || `#${r.index}`}</span>
							{#if r.uncertainty != null}
								<span
									class="text-muted-foreground shrink-0 text-xs tabular-nums"
									title="uncertainty"
								>
									{r.uncertainty.toFixed(2)}
								</span>
							{/if}
						</span>
						{#if range}
							<span
								class="text-muted-foreground block font-mono text-xs tabular-nums"
								title="segment time"
							>
								{range}
							</span>
						{/if}
						{#if r.text}
							<span class="text-muted-foreground block truncate text-xs">{r.text}</span>
						{/if}
					</span>
				</button>
			</li>
		{:else}
			<li class="text-muted-foreground px-3 py-6 text-center text-sm">No annotations</li>
		{/each}
	</ul>
</div>
