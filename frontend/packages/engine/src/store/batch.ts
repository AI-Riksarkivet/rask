/**
 * Arrow batch construction — the one engine primitive the annotator's optimistic
 * append path uses. (Extracted from the removed AnnotationStore — see the note in
 * engine/index.ts — so the live code carries none of that dead weight.)
 */
import {
	type Data,
	makeBuilder,
	makeData,
	RecordBatch,
	type Schema,
	Struct,
	type Table,
	Table as ArrowTable,
} from 'apache-arrow';

/**
 * Build a Table containing only `rows`, using `schema`'s EXACT field types
 * (Float32 stays Float32, Dictionary<Utf8> appends a delta, List<Float32> kept).
 * Because the schema matches the base, the result `concat`s onto it cleanly —
 * O(rows), not O(total). Used by interactive appends AND streamed inference.
 */
export function buildBatchTable(schema: Schema, rows: Record<string, unknown>[]): Table {
	const children: Data[] = [];
	for (const field of schema.fields) {
		const builder = makeBuilder({
			type: field.type,
			nullValues: [null, undefined],
		});
		for (const row of rows) builder.append(row[field.name] ?? null);
		children.push(builder.finish().flush());
	}
	const structData = makeData({
		type: new Struct(schema.fields),
		length: rows.length,
		children,
	});
	return new ArrowTable(schema, new RecordBatch(schema, structData));
}
