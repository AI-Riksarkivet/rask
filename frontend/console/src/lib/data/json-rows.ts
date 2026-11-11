import { Int64, Table, tableToIPC, vectorFromArray, type Vector } from 'apache-arrow';
import * as v from 'valibot';

// JSON rows typed in a form → an Arrow IPC stream, for the catalog's create and insert doors. JSON has
// one number type, and apache-arrow's own inference (`tableFromJSON`) reads every number as Float64; on
// a create the first rows fix the table's schema for good, so a column whose every value is a whole
// number in the safe integer range is written as Int64. Every other column keeps apache-arrow's
// inference. An insert is cast to the table's schema by the catalog, so the same encoding serves it.
// Encoding builds no code at runtime: the null check apache-arrow's builders use is the closure
// `patches/apache-arrow@21.2.0.patch` puts in place of a compiled function, which the console's CSP
// would refuse.

/** Rows as a form takes them: a non-empty JSON array of objects. */
const RowsSchema = v.pipe(
	v.array(v.record(v.string(), v.unknown()), 'expected a JSON array of row objects'),
	v.minLength(1, 'expected at least one row'),
);
export type JsonRows = v.InferOutput<typeof RowsSchema>;

/** The rows in `text`, or why they are not rows. */
export function parseJsonRows(
	text: string,
): { ok: true; rows: JsonRows } | { ok: false; reason: string } {
	let parsed: unknown;
	try {
		parsed = JSON.parse(text);
	} catch (e) {
		return { ok: false, reason: e instanceof Error ? e.message : String(e) };
	}
	const result = v.safeParse(RowsSchema, parsed);
	return result.success
		? { ok: true, rows: result.output }
		: { ok: false, reason: result.issues[0].message };
}

const isWholeNumber = (value: unknown): value is number =>
	typeof value === 'number' && Number.isSafeInteger(value);

/** One column's vector: Int64 when every present value is a whole number, else inferred. */
function columnVector(values: unknown[]): Vector {
	const present = values.filter((value) => value !== null && value !== undefined);
	if (present.length > 0 && present.every(isWholeNumber)) {
		return vectorFromArray(
			values.map((value) => (isWholeNumber(value) ? BigInt(value) : null)),
			new Int64(),
		);
	}
	return vectorFromArray(values);
}

/** The column names in first-seen order across every row, as `tableFromJSON` takes them. */
function columnNames(rows: JsonRows): string[] {
	const names = new Set<string>();
	for (const row of rows) for (const key of Object.keys(row)) names.add(key);
	return [...names];
}

/** The Arrow table for `rows`, a column per key. */
export function rowsToTable(rows: JsonRows): Table {
	const columns: Record<string, Vector> = {};
	for (const name of columnNames(rows))
		columns[name] = columnVector(rows.map((row) => row[name] ?? null));
	return new Table(columns);
}

/** The Arrow IPC stream the catalog's create and insert doors take. */
export const rowsToArrow = (rows: JsonRows): Uint8Array => tableToIPC(rowsToTable(rows), 'stream');
