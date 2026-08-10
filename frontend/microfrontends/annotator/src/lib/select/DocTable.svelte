<script lang="ts">
	// The corpus browser's DEFAULT view — documents as a filterable, sortable table.
	//
	// Reported: "why does browse corpus looks so fucking wierd? why does it show like gallery view
	// initally?"
	//
	// The rail calls this row "Bulk labeling" and #42 made it the bulk-labeling surface: its job is
	// "find five hundred pages and send them". It opened on a gallery of one-document tiles, so that
	// job had to be done by eye, a tile at a time, with nothing to sort or filter by. The Data
	// Manager shape already landed for the QUEUE (#58/#59); this is that shape one level up, over
	// documents.
	//
	// The gallery is not deleted — it is a toggle. Looking at pages IS the right view when you are
	// choosing between two similar documents; it was only ever wrong as the DEFAULT for a surface
	// named after bulk work.
	import { ArrowDown, ArrowUp, ChevronsUpDown } from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { Input } from '@rask/ui/input';
	import * as Table from '@rask/ui/table';
	import { cn } from '@rask/ui/utils';
	import type { DatasetView } from '@rask/explorer-api/descriptor';

	import { compareDocs, docCellText, docColumns, DOC_ID } from './doc-columns';

	let {
		view,
		docs,
		onopen,
	}: {
		view: DatasetView;
		docs: Record<string, unknown>[];
		/** Open one document — the same drill-in the gallery tile performs. */
		onopen: (doc: Record<string, unknown>) => void;
	} = $props();

	let query = $state('');
	let sortField = $state<string>(DOC_ID);
	let sortAsc = $state(true);

	/** The explicit adapter from `DatasetView` to this module's `DocView`.
	 *
	 *  Spelled out rather than passing `view` straight through, because the two are only
	 *  STRUCTURALLY related and the first cut got that wrong silently — it named a `docTitle` method
	 *  no corpus has, and TypeScript accepted the mismatch because the member was optional. Naming
	 *  each mapping here means a rename on either side is a compile error rather than a blank
	 *  column. */
	const docView = $derived({
		docId: (r: Record<string, unknown>) => view.docId(r),
		title: (r: Record<string, unknown>) => view.title(r),
		titleFields: view.titleFields,
		metadataFields: view.metadataFields,
	});

	const columns = $derived(docColumns(docView));

	// Filter THEN sort, both derived: the table is a view of `docs`, never a copy of it, so a page
	// change upstream cannot leave stale rows on screen.
	const rows = $derived(
		docs
			.filter((d) => {
				const q = query.trim().toLowerCase();
				if (!q) return true;
				return columns.some((c) => docCellText(docView, d, c.field).toLowerCase().includes(q));
			})
			.toSorted((a, b) => (sortAsc ? 1 : -1) * compareDocs(docView, a, b, sortField)),
	);

	function sortBy(field: string): void {
		if (sortField === field) sortAsc = !sortAsc;
		else {
			sortField = field;
			sortAsc = true;
		}
	}
</script>

<div class="flex min-h-0 flex-1 flex-col gap-2">
	<Input
		bind:value={query}
		placeholder="Filter documents…"
		class="h-8 max-w-xs"
		aria-label="Filter documents"
		data-testid="doc-filter"
	/>

	<!-- The table scrolls, the page does not: the filter and the pager below must stay reachable
	     while you work a long list. -->
	<div class="min-h-0 flex-1 overflow-auto rounded-md border">
		<Table.Root data-testid="doc-table">
			<Table.Header>
				<Table.Row>
					{#each columns as col (col.field)}
						<Table.Head>
							<Button
								variant="ghost"
								size="sm"
								class="-ml-2 h-7 px-2"
								onclick={() => sortBy(col.field)}
								data-testid="doc-sort-{col.field}"
							>
								{col.label}
								{#if sortField !== col.field}
									<ChevronsUpDown class="size-3 opacity-40" />
								{:else if sortAsc}
									<ArrowUp class="size-3" />
								{:else}
									<ArrowDown class="size-3" />
								{/if}
							</Button>
						</Table.Head>
					{/each}
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each rows as doc (view.docId(doc))}
					<Table.Row class="cursor-pointer" onclick={() => onopen(doc)} data-testid="doc-row">
						{#each columns as col (col.field)}
							<Table.Cell class={cn(col.field === DOC_ID && 'font-mono text-xs')}>
								{docCellText(docView, doc, col.field)}
							</Table.Cell>
						{/each}
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>

		{#if rows.length === 0}
			<!-- Names WHY it is empty. "No documents" would be a lie when the corpus has thousands and
			     a filter is hiding them — and the fix (clear the filter) is invisible otherwise. -->
			<p class="text-muted-foreground p-6 text-center text-sm" data-testid="doc-table-empty">
				{docs.length === 0
					? 'No documents in this corpus.'
					: `No document on this page matches “${query}”.`}
			</p>
		{/if}
	</div>
</div>
