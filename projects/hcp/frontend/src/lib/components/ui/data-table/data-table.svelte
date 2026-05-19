<script lang="ts" generics="TData">
	import type { Snippet } from 'svelte';
	import type { Table } from '@tanstack/table-core';
	import * as TablePrimitive from '$lib/components/ui/table/index.js';
	import { FlexRender } from '$lib/components/ui/data-table/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import ChevronLeft from 'lucide-svelte/icons/chevron-left';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import { cn } from '$lib/utils.js';

	let {
		table,
		onrowclick,
		noResultsMessage = 'No results.',
		subHeaderRow,
		footer,
	}: {
		table: Table<TData>;
		onrowclick?: (row: TData) => void;
		noResultsMessage?: string;
		subHeaderRow?: Snippet;
		footer?: Snippet;
	} = $props();

	const PAGE_SIZES = [25, 50, 100, 500];

	let pageCount = $derived(table.getPageCount());
	let pageIndex = $derived(table.getState().pagination.pageIndex);
	let pageSize = $derived(String(table.getState().pagination.pageSize));
	let totalRows = $derived(table.getCoreRowModel().rows.length);
	let showPagination = $derived(totalRows > PAGE_SIZES[0]);
</script>

<div class="overflow-x-auto rounded-lg border">
	<TablePrimitive.Root>
		<TablePrimitive.Header
			class="bg-muted/50 text-xs uppercase tracking-wide text-muted-foreground"
		>
			{#each table.getHeaderGroups() as headerGroup (headerGroup.id)}
				<TablePrimitive.Row class="border-b hover:bg-transparent">
					{#each headerGroup.headers as header (header.id)}
						<TablePrimitive.Head
							class={cn('px-4 py-3 font-medium', header.column.columnDef.meta?.headerClass)}
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
			{#if subHeaderRow}
				{@render subHeaderRow()}
			{/if}
		</TablePrimitive.Header>
		<TablePrimitive.Body>
			{#each table.getRowModel().rows as row (row.id)}
				<TablePrimitive.Row
					class={cn('bg-card transition-colors', onrowclick && 'cursor-pointer')}
					onclick={onrowclick ? () => onrowclick(row.original) : undefined}
					onkeydown={onrowclick
						? (e) => {
								if (e.key === 'Enter') onrowclick(row.original);
							}
						: undefined}
					role={onrowclick ? 'button' : undefined}
					tabindex={onrowclick ? 0 : undefined}
				>
					{#each row.getVisibleCells() as cell (cell.id)}
						<TablePrimitive.Cell class={cn('px-4 py-3', cell.column.columnDef.meta?.cellClass)}>
							<FlexRender content={cell.column.columnDef.cell} context={cell.getContext()} />
						</TablePrimitive.Cell>
					{/each}
				</TablePrimitive.Row>
			{:else}
				<TablePrimitive.Row>
					<TablePrimitive.Cell
						colspan={table.getAllColumns().length}
						class="px-4 py-8 text-center text-muted-foreground"
					>
						{noResultsMessage}
					</TablePrimitive.Cell>
				</TablePrimitive.Row>
			{/each}
		</TablePrimitive.Body>
	</TablePrimitive.Root>
</div>

{#if showPagination || footer}
	<div class="flex items-center justify-between py-2">
		<div class="flex items-center gap-4 text-sm text-muted-foreground">
			{#if footer}
				{@render footer()}
			{/if}
		</div>
		{#if showPagination}
			<div class="flex items-center gap-3">
				<div class="flex items-center gap-1.5">
					<span class="text-xs text-muted-foreground">Rows</span>
					<select
						class="border-input bg-background text-foreground ring-offset-background focus:ring-ring flex h-7 w-auto min-w-[60px] items-center rounded-md border px-2 py-0.5 text-xs shadow-sm focus:outline-none focus:ring-1 disabled:cursor-not-allowed disabled:opacity-50"
						value={pageSize}
						onchange={(e) => table.setPageSize(Number(e.currentTarget.value))}
					>
						{#each PAGE_SIZES as size (size)}
							<option value={String(size)}>{size}</option>
						{/each}
					</select>
				</div>
				<span class="text-xs text-muted-foreground">
					Page {pageIndex + 1} of {pageCount}
				</span>
				<Button
					variant="outline"
					size="icon"
					class="h-8 w-8"
					onclick={() => table.previousPage()}
					disabled={!table.getCanPreviousPage()}
				>
					<ChevronLeft class="h-4 w-4" />
				</Button>
				<Button
					variant="outline"
					size="icon"
					class="h-8 w-8"
					onclick={() => table.nextPage()}
					disabled={!table.getCanNextPage()}
				>
					<ChevronRight class="h-4 w-4" />
				</Button>
			</div>
		{/if}
	</div>
{/if}
