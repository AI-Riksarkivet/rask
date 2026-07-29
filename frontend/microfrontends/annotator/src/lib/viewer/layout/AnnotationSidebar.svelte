<script lang="ts">
	// Right inspector pane. Composes the review list / single-annotation detail
	// + the layer panel. Controlled by the facade. (Ported from ra-anno, split into
	// focused sub-components per our no-god-files rule.)
	import { ChevronLeft, List, Table } from '@lucide/svelte';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { cn } from '@rask/ui/utils';
	import { statusDot } from './statusStyle';
	import AnnotationDetail from './AnnotationDetail.svelte';
	import AnnotationList from './AnnotationList.svelte';
	import AnnotationTable from './AnnotationTable.svelte';
	import BulkActions from './BulkActions.svelte';
	import LayerPanel from './LayerPanel.svelte';
	import VersionHistoryPanel from './VersionHistoryPanel.svelte';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	// browse mode when nothing is selected: compact list vs full sortable table
	let view = $state<'list' | 'table'>('list');

	const summary = $derived.by(() => {
		const counts = new Map<string, number>();
		for (const r of controller.rows) counts.set(r.status, (counts.get(r.status) ?? 0) + 1);
		return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
	});
</script>

<aside
	class="border-border bg-card flex h-full w-full min-w-0 flex-col border-l"
	data-testid="annotation-sidebar"
>
	<header class="border-border flex items-center justify-between gap-2 border-b px-3 py-2">
		<h2 class="text-sm font-medium">Review queue</h2>
		<div class="flex items-center gap-2">
			<!-- A segmented list/table switch: one bordered group, ghost buttons inside, so it reads
			     like the estate's other view toggles rather than two loose icon buttons. -->
			<div class="border-border flex overflow-hidden rounded-lg border p-0.5">
				<Button
					variant={view === 'list' ? 'secondary' : 'ghost'}
					size="icon-xs"
					title="List"
					aria-pressed={view === 'list'}
					onclick={() => (view = 'list')}
				>
					<List class="size-3.5" />
				</Button>
				<Button
					variant={view === 'table' ? 'secondary' : 'ghost'}
					size="icon-xs"
					title="Table"
					aria-pressed={view === 'table'}
					onclick={() => (view = 'table')}
				>
					<Table class="size-3.5" />
				</Button>
			</div>
			<Badge variant="outline" class="tabular-nums" title="Annotations on this unit">
				{controller.count}
			</Badge>
		</div>
	</header>

	<!-- status summary -->
	<div class="border-border flex flex-wrap gap-x-3 gap-y-1.5 border-b px-3 py-2">
		{#each summary as [status, n] (status)}
			<span class="text-muted-foreground flex items-center gap-1.5 text-xs">
				<span class={cn('size-2 rounded-full', statusDot(status))}></span>
				{status || '—'} <span class="text-foreground tabular-nums">{n}</span>
			</span>
		{/each}
	</div>

	<div class="flex min-h-0 flex-1 flex-col">
		{#if controller.multiSelect}
			{#if controller.selectedSet.size > 1}
				<BulkActions {controller} />
			{/if}
			<div class="min-h-0 flex-1 overflow-y-auto">
				<AnnotationList {controller} />
			</div>
		{:else if controller.selected}
			<div class="border-border border-b px-2 py-1.5">
				<Button variant="ghost" size="sm" onclick={() => controller.select(null)}>
					<ChevronLeft class="size-3.5" /> Back to list
				</Button>
			</div>
			<div class="min-h-0 flex-1 overflow-y-auto">
				<AnnotationDetail {controller} />
			</div>
		{:else if view === 'table'}
			<AnnotationTable {controller} />
		{:else}
			<AnnotationList {controller} />
		{/if}
	</div>

	<LayerPanel {controller} />
	<VersionHistoryPanel {controller} />
</aside>
