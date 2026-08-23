<script lang="ts" generics="TData">
	import type { Snippet } from 'svelte';
	import type { Table } from '@tanstack/table-core';
	import { ChevronLeft, ChevronRight, TriangleAlert } from '@lucide/svelte';
	import * as TablePrimitive from '../table/index.js';
	import { Button } from '../button/index.js';
	import { Skeleton } from '../skeleton/index.js';
	import { cn } from '../../utils/cn.js';
	import FlexRender from './flex-render.svelte';
	import './types.js';

	// The shared table shell around a `createSvelteTable` instance: header/body from the column
	// defs (via FlexRender), explicit loading (skeleton rows) / error / empty states, an optional
	// row-click, a footer snippet (selection counts etc.) and pagination controls that appear only
	// once the data outgrows the smallest page size. Presentation-only — sorting/filtering/selection
	// live in the consumer's table state, so this renders ANY tanstack feature combination.
	let {
		table,
		loading = false,
		error = null,
		emptyMessage = 'No results.',
		onrowclick,
		footer,
	}: {
		table: Table<TData>;
		/** True while the first load is in flight — renders skeleton rows instead of the body. */
		loading?: boolean;
		/** A load failure to surface in-table (the row area shows it; header stays for context). */
		error?: string | null;
		emptyMessage?: string;
		onrowclick?: (row: TData) => void;
		footer?: Snippet;
	} = $props();

	const PAGE_SIZES = [10, 25, 50, 100];

	const columnCount = $derived(table.getAllColumns().length);
	const pageCount = $derived(table.getPageCount());
	const pageIndex = $derived(table.getState().pagination.pageIndex);
	const pageSize = $derived(String(table.getState().pagination.pageSize));
	const totalRows = $derived(table.getCoreRowModel().rows.length);
	const showPagination = $derived(totalRows > (PAGE_SIZES[0] ?? 10));
</script>

<!-- The table CARD owns containment: `min-w-0` + `max-w-full` so a wide table can never push the
     page (and with it the document) past the viewport, and `overflow-hidden` so the rounded border
     clips cleanly. The horizontal scroll itself belongs to the inner `table-container` the Table
     primitive already provides — the wide table scrolls INSIDE this card instead of slicing the
     card off the right edge of the screen. -->
<div
	data-slot="data-table-card"
	class="border-border w-full max-w-full min-w-0 overflow-hidden rounded-lg border"
>
	<TablePrimitive.Root>
		<TablePrimitive.Header class="bg-muted/50">
			{#each table.getHeaderGroups() as headerGroup (headerGroup.id)}
				<TablePrimitive.Row class="hover:bg-transparent">
					{#each headerGroup.headers as header (header.id)}
						<TablePrimitive.Head
							class={cn(
								'text-muted-foreground px-3 py-2 font-medium',
								header.column.columnDef.meta?.headerClass,
							)}
						>
							{#if !header.isPlaceholder}
								<FlexRender
									content={header.column.columnDef.header}
									context={header.getContext()}
								/>
							{/if}
						</TablePrimitive.Head>
					{/each}
				</TablePrimitive.Row>
			{/each}
		</TablePrimitive.Header>
		<TablePrimitive.Body>
			{#if loading}
				{#each [0, 1, 2, 3, 4] as i (i)}
					<TablePrimitive.Row>
						<TablePrimitive.Cell colspan={columnCount} class="px-3 py-2.5">
							<Skeleton class="h-4 w-full" />
						</TablePrimitive.Cell>
					</TablePrimitive.Row>
				{/each}
			{:else if error !== null}
				<TablePrimitive.Row>
					<TablePrimitive.Cell
						colspan={columnCount}
						class="px-3 py-8 text-center break-words whitespace-normal"
					>
						<div class="text-destructive inline-flex items-center gap-2 text-sm">
							<TriangleAlert class="size-4 shrink-0" />
							{error}
						</div>
					</TablePrimitive.Cell>
				</TablePrimitive.Row>
			{:else}
				{#each table.getRowModel().rows as row (row.id)}
					<TablePrimitive.Row
						data-state={row.getIsSelected() ? 'selected' : undefined}
						class={cn(onrowclick && 'cursor-pointer')}
						onclick={onrowclick ? () => onrowclick(row.original) : undefined}
						onkeydown={onrowclick
							? (e: KeyboardEvent) => {
									if (e.key === 'Enter') onrowclick(row.original);
								}
							: undefined}
						role={onrowclick ? 'button' : undefined}
						tabindex={onrowclick ? 0 : undefined}
					>
						{#each row.getVisibleCells() as cell (cell.id)}
							<TablePrimitive.Cell class={cn('px-3 py-2', cell.column.columnDef.meta?.cellClass)}>
								<FlexRender content={cell.column.columnDef.cell} context={cell.getContext()} />
							</TablePrimitive.Cell>
						{/each}
					</TablePrimitive.Row>
				{:else}
					<TablePrimitive.Row>
						<!-- `whitespace-normal`: the Cell primitive is nowrap by default (right for data
						     cells, wrong for a sentence) — without it a long empty-state message widens the
						     table past its card and reads as text sliced mid-word. -->
						<TablePrimitive.Cell
							colspan={columnCount}
							class="text-muted-foreground px-3 py-8 text-center break-words whitespace-normal"
						>
							{emptyMessage}
						</TablePrimitive.Cell>
					</TablePrimitive.Row>
				{/each}
			{/if}
		</TablePrimitive.Body>
	</TablePrimitive.Root>
</div>

{#if footer || showPagination}
	<!-- Wraps rather than overflows: on a narrow viewport the footer counts and the pagination
	     controls stack instead of pushing the page sideways. -->
	<div class="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 py-2">
		<div class="text-muted-foreground flex min-w-0 items-center gap-4 text-sm">
			{#if footer}
				{@render footer()}
			{/if}
		</div>
		{#if showPagination}
			<div class="ml-auto flex items-center gap-3">
				<label class="text-muted-foreground flex items-center gap-1.5 text-xs">
					Rows
					<select
						class="border-input bg-background text-foreground focus-visible:border-ring focus-visible:ring-ring/50 h-7 rounded-lg border px-2 py-0.5 text-xs transition-colors outline-none focus-visible:ring-3"
						value={pageSize}
						onchange={(e) => table.setPageSize(Number(e.currentTarget.value))}
					>
						{#each PAGE_SIZES as size (size)}
							<option value={String(size)}>{size}</option>
						{/each}
					</select>
				</label>
				<span class="text-muted-foreground text-xs tabular-nums">
					Page {pageIndex + 1} of {Math.max(pageCount, 1)}
				</span>
				<Button
					variant="outline"
					size="icon-sm"
					aria-label="Previous page"
					onclick={() => table.previousPage()}
					disabled={!table.getCanPreviousPage()}
				>
					<ChevronLeft class="size-4" />
				</Button>
				<Button
					variant="outline"
					size="icon-sm"
					aria-label="Next page"
					onclick={() => table.nextPage()}
					disabled={!table.getCanNextPage()}
				>
					<ChevronRight class="size-4" />
				</Button>
			</div>
		{/if}
	</div>
{/if}
