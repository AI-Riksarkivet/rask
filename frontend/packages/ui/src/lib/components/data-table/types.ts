import type { RowData } from '@tanstack/table-core';

/** Per-column presentation hooks DataTable applies to the <th>/<td> it renders, so a column can
 *  size/align itself from its ColumnDef (`meta: { headerClass: 'w-10' }`) without the consumer
 *  re-implementing the table markup. */
export type DataTableColumnMeta = {
	headerClass?: string;
	cellClass?: string;
};

declare module '@tanstack/table-core' {
	// Module augmentation must repeat the interface's type parameters verbatim.
	// eslint-disable-next-line @typescript-eslint/no-unused-vars
	interface ColumnMeta<TData extends RowData, TValue> extends DataTableColumnMeta {}
}
