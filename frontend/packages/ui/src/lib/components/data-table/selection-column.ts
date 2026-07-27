import type { ColumnDef, RowData } from '@tanstack/table-core';
import DataTableCheckbox from './data-table-checkbox.svelte';
import { renderComponent } from './render-helpers.js';
import './types.js';

/**
 * The checkbox selection column, ready to prepend to any column set (pair it with
 * `enableRowSelection: true` + a stable `getRowId` so selection survives sorting/paging):
 * header = select-all-on-page (indeterminate when partial), cell = the row's checkbox.
 */
export function selectionColumn<TData extends RowData>(): ColumnDef<TData> {
	return {
		id: 'select',
		header: ({ table }) =>
			renderComponent(DataTableCheckbox, {
				checked: table.getIsAllPageRowsSelected(),
				indeterminate: table.getIsSomePageRowsSelected() && !table.getIsAllPageRowsSelected(),
				onCheckedChange: (checked: boolean) => table.toggleAllPageRowsSelected(checked),
				'aria-label': 'Select all rows',
			}),
		cell: ({ row }) =>
			renderComponent(DataTableCheckbox, {
				checked: row.getIsSelected(),
				onCheckedChange: (checked: boolean) => row.toggleSelected(checked),
				'aria-label': 'Select row',
			}),
		enableSorting: false,
		enableHiding: false,
		meta: { headerClass: 'w-10', cellClass: 'w-10' },
	};
}
