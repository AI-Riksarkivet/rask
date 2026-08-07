import DataTableHeaderButton from './data-table-header-button.svelte';
import { renderComponent } from './render-helpers.js';

/** The sortable column HEADER — label + sort chevron, wired to TanStack's toggle handler. Six zone
 *  tables carried this exact closure verbatim (#96); a `ColumnDef`'s `header` points at it
 *  directly: `header: sortableHeader('when')`. The column parameter is structural (the two methods
 *  actually read) rather than TanStack's `HeaderContext`, for the same reason the zones' copies
 *  were: the render call must not force a generic argument the caller never uses. */
export const sortableHeader =
	(label: string) =>
	({
		column,
	}: {
		column: {
			getIsSorted(): false | 'asc' | 'desc';
			getToggleSortingHandler(): ((e: Event) => void) | undefined;
		};
	}) =>
		renderComponent(DataTableHeaderButton, {
			label,
			sorted: column.getIsSorted(),
			onclick: column.getToggleSortingHandler(),
		});
