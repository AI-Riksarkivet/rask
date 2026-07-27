import type { Table } from 'apache-arrow';

/** Extract sorted unique non-null string values from a table column */
export function uniqueColumnValues(table: Table, columnName: string): string[] {
	const col = table.getChild(columnName);
	if (!col) return [];
	const set = new Set<string>();
	for (let i = 0; i < table.numRows; i++) {
		const v = col.get(i);
		if (v) set.add(String(v));
	}
	return [...set].sort();
}
