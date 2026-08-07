/**
 * Tag→annotation writer — the read plane's chunk-tags promoted to real annotation ROWS.
 *
 * Framework-agnostic (no Svelte): maps tagged rows → a per-row-identity TagBatch and
 * POSTs it to /api/annotations/tags, where the backend writes one merge_insert version
 * (shape_type="tag", label=<tag>, geometry zeroed, human provenance, author server-side).
 * Arity-generic off the descriptor's identity `keyFields` — never hardcodes
 * doc/speech/chunk. This unifies the workflow tag store with the annotations table so a
 * chunk tag is a first-class, reviewable, versioned annotation. See labeling/types.ts.
 */

/** A chunk (or unit) + the tag labels to set. `keys` = the NON-doc identity fields,
 *  positional (pairs with keyFields minus the doc key). */
export interface TagWrite {
	doc_id: string;
	keys: number[];
	labels: string[];
}

export interface TagBatch {
	adds: TagWrite[];
	removes?: TagWrite[];
	base_version?: number | null;
}

export interface SaveResult {
	saved: number;
	version: number;
}

/**
 * Map tagged rows → a TagBatch. `keyFields` = the descriptor identity key_fields
 * (`[docKey, ...rest]`); `tagsFor(row)` returns the labels on that row. Untagged rows
 * are skipped (no empty write). Identity is read from the RAW key columns (never from a
 * joined hitKey — lossy when a value contains the separator).
 */
export function tagBatchFromTaggedHits(
	rows: readonly Record<string, unknown>[],
	keyFields: readonly string[],
	tagsFor: (row: Record<string, unknown>) => readonly string[],
): TagBatch {
	// An EMPTY keyFields has no doc column — indexing rows by `undefined` would silently read the
	// literal "undefined" key; the sibling `tagRemovesFromEntries` already guards the same way.
	const [docKey = '', ...rest] = keyFields;
	const adds: TagWrite[] = [];
	for (const row of rows) {
		const labels = tagsFor(row);
		if (!labels.length) continue;
		adds.push({
			doc_id: String(row[docKey] ?? ''),
			keys: rest.map((k) => Number(row[k])),
			labels: [...labels],
		});
	}
	return { adds };
}

/**
 * Map queued un-tags → the batch's `removes` leg, with the same raw-key-column
 * identity mapping as the adds. Entries carry the hit captured at removal time.
 */
export function tagRemovesFromEntries(
	entries: readonly { hit: Record<string, unknown>; labels: readonly string[] }[],
	keyFields: readonly string[],
): TagWrite[] {
	const [docKey, ...rest] = keyFields;
	return entries
		.filter((e) => e.labels.length)
		.map((e) => ({
			doc_id: String(e.hit[docKey ?? ''] ?? ''),
			keys: rest.map((k) => Number(e.hit[k])),
			labels: [...e.labels],
		}));
}

// The fetch transport that used to live here (`saveTagsAsAnnotations`) is DELETED (#94): the one
// caller (explorer's ExportNode) rides the zone's remote function of the same name, and a dead
// transport with an `as SaveResult` cast is drift waiting to happen. The batch builders above and
// the `SaveResult` contract are what this module is.
