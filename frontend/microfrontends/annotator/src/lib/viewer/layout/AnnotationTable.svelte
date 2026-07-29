<script lang="ts">
	// Full columns/sort table of the unit's annotations — the review queue as a
	// spreadsheet (ported from the pre-monorepo AnnotationTable, commit 6afcbc7, which
	// the apps/media move dropped). Reads the controller's rows (no own fetch); click a
	// row to select it on the canvas; click a header to sort. Built from @rask/ui's Table
	// parts + SortHeader, so it IS the same table object the data/admin registries render.
	import * as Table from '@rask/ui/table';
	import { SortHeader } from '@rask/ui/sort-header';
	import { cn } from '@rask/ui/utils';
	import { statusDot } from './statusStyle';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	type SortKey = 'label' | 'shape' | 'status' | 'confidence' | 'uncertainty';
	const NUMERIC: SortKey[] = ['confidence', 'uncertainty'];
	let sortKey = $state<SortKey>('uncertainty');
	let sortDir = $state<1 | -1>(-1);

	// SortHeader hands back the column id as a string; the ids come from `cols` below, so the
	// narrowing is a cast, not a guess.
	function toggleSort(col: string): void {
		const k = col as SortKey;
		if (sortKey === k) sortDir = sortDir === 1 ? -1 : 1;
		else {
			sortKey = k;
			sortDir = NUMERIC.includes(k) ? -1 : 1;
		}
	}

	const sorted = $derived(
		controller.rows.toSorted((a, b) => {
			const av = a[sortKey] ?? '';
			const bv = b[sortKey] ?? '';
			if (typeof av === 'number' || typeof bv === 'number') {
				return ((av as number) - (bv as number)) * sortDir;
			}
			return String(av).localeCompare(String(bv)) * sortDir;
		}),
	);

	const cols: { key: SortKey; label: string; num?: boolean }[] = [
		{ key: 'label', label: 'Label' },
		{ key: 'shape', label: 'Shape' },
		{ key: 'status', label: 'Status' },
		{ key: 'confidence', label: 'Conf', num: true },
		{ key: 'uncertainty', label: 'Unc', num: true },
	];
	const fmt = (n: number | null): string => (n == null ? '—' : n.toFixed(2));
</script>

<div class="flex min-h-0 flex-1 flex-col overflow-auto" data-testid="annotation-table">
	<Table.Root class="text-xs">
		<Table.Header class="bg-muted/70 sticky top-0 z-10 backdrop-blur">
			<Table.Row class="hover:bg-transparent">
				{#each cols as c (c.key)}
					<SortHeader
						label={c.label}
						col={c.key}
						{sortKey}
						sortDir={sortDir === 1 ? 'asc' : 'desc'}
						onsort={toggleSort}
						class={cn('px-2 py-1.5 text-xs', c.num && 'text-right')}
					/>
				{/each}
			</Table.Row>
		</Table.Header>
		<Table.Body>
			{#each sorted as r (r.index)}
				<Table.Row
					class={cn('cursor-pointer', controller.selectedIndex === r.index && 'bg-primary/10')}
					onclick={() => controller.select(r.index)}
				>
					<Table.Cell class="max-w-28 truncate px-2 py-1" title={r.label}>
						{r.label || `#${r.index}`}
					</Table.Cell>
					<Table.Cell class="text-muted-foreground px-2 py-1">{r.shape || '—'}</Table.Cell>
					<Table.Cell class="px-2 py-1">
						<span class="inline-flex items-center gap-1.5">
							<span class={cn('size-2 rounded-full', statusDot(r.status))}></span>
							{r.status || '—'}
						</span>
					</Table.Cell>
					<Table.Cell class="text-muted-foreground px-2 py-1 text-right tabular-nums">
						{fmt(r.confidence)}
					</Table.Cell>
					<Table.Cell class="text-muted-foreground px-2 py-1 text-right tabular-nums">
						{fmt(r.uncertainty)}
					</Table.Cell>
				</Table.Row>
			{:else}
				<Table.Row class="hover:bg-transparent">
					<Table.Cell colspan={cols.length} class="text-muted-foreground px-3 py-6 text-center">
						No annotations
					</Table.Cell>
				</Table.Row>
			{/each}
		</Table.Body>
	</Table.Root>
</div>
