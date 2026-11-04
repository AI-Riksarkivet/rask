/** One field of the table's Arrow schema, as the detail aggregate serves it. */
export interface SchemaField {
	name: string;
	type?: unknown;
	nullable?: boolean;
	metadata?: Record<string, string>;
}

/** The display name of a schema field's Arrow type — the wire nests it as `{type: …}` for complex
 *  types and hands a bare value for scalars. Shared by the schema table and the blob-column filter. */
export function typeName(t: unknown): string {
	if (t && typeof t === 'object' && 'type' in t) return String((t as { type: unknown }).type);
	return String(t ?? '—');
}
