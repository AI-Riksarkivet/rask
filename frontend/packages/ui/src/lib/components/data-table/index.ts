// The @rask/ui data-table stack: a Svelte 5 adapter over @tanstack/table-core (createSvelteTable),
// the shared table shell (DataTable: states + pagination), the column building blocks (sortable
// header button, text filter, selection column, row-actions slot) and the render helpers that let
// ColumnDefs point at Svelte components/snippets. Import the row models straight from here so
// consumers need no direct @tanstack dependency.
export { default as DataTable } from './data-table.svelte';
export { default as DataTableHeaderButton } from './data-table-header-button.svelte';
export { default as DataTableCheckbox } from './data-table-checkbox.svelte';
export { default as DataTableTextFilter } from './data-table-text-filter.svelte';
export { default as DataTableRowActions } from './data-table-row-actions.svelte';
export { default as FlexRender } from './flex-render.svelte';
export { createSvelteTable, mergeObjects } from './create-svelte-table.svelte.js';
export { renderComponent, renderSnippet } from './render-helpers.js';
export { selectionColumn } from './selection-column.js';
export {
	createColumnHelper,
	getCoreRowModel,
	getFilteredRowModel,
	getPaginationRowModel,
	getSortedRowModel,
} from '@tanstack/table-core';
export type {
	CellContext,
	ColumnDef,
	HeaderContext,
	PaginationState,
	RowSelectionState,
	SortingState,
	Table,
} from '@tanstack/table-core';
// A TYPE re-export (not a side-effect import) so the ColumnMeta augmentation in types.ts reaches
// consumers through the emitted d.ts chain.
export type { DataTableColumnMeta } from './types.js';
