/**
 * Annotation history — the read-side of the write-plane provenance story (who=reviewer,
 * when=version, what=lineage). Framework-agnostic (no Svelte): fetches a unit's Lance
 * version history + a historical snapshot, and diffs a past version against the current
 * rows by id. The compare-versions panel is a thin view over this. See annotate.py's
 * /versions endpoint + the ?version time-travel read.
 */
import { tableFromIPC } from 'apache-arrow';

export interface AnnotationVersion {
	version: number;
	timestamp: string;
	count: number;
}

/** The review-visible fields a diff compares (geometry moves are not "review" changes). */
export interface RowSig {
	label: string;
	status: string;
	shape: string;
	group: string;
	text: string;
}

/** A stable signature of a row's reviewable content — equal signatures ⇒ no change.
 *  JSON-encode the field array so it's provably injective: no separator to collide
 *  ("ab","" vs "a","b" encode differently) and any value (quotes, control chars) is safe. */
export function rowSignature(r: RowSig): string {
	return JSON.stringify([
		r.label ?? '',
		r.status ?? '',
		r.shape ?? '',
		r.group ?? '',
		r.text ?? '',
	]);
}

/** Insert `suffix` into the annotations path (before any query) and merge `extra` params —
 *  so `/api/annotations/a/b/c?dataset=X` → `/api/annotations/a/b/c/versions?dataset=X&limit=20`. */
function buildUrl(annotationsUrl: string, suffix: string, extra: Record<string, string>): string {
	const [path, search = ''] = annotationsUrl.split('?');
	const params = new URLSearchParams(search);
	for (const [k, val] of Object.entries(extra)) params.set(k, val);
	const qs = params.toString();
	return `${path}${suffix}${qs ? `?${qs}` : ''}`;
}

/** The unit's edit history (most-recent first, capped). */
export async function fetchVersions(
	annotationsUrl: string,
	limit = 20,
): Promise<AnnotationVersion[]> {
	const res = await fetch(buildUrl(annotationsUrl, '/versions', { limit: String(limit) }));
	if (!res.ok) throw new Error(`versions failed (HTTP ${res.status})`);
	return (await res.json()) as AnnotationVersion[];
}

/** A historical version's rows as id→signature (for diffing against the current rows). */
export async function fetchVersionSignatures(
	annotationsUrl: string,
	version: number,
): Promise<Map<string, string>> {
	const res = await fetch(buildUrl(annotationsUrl, '', { version: String(version) }));
	if (!res.ok) throw new Error(`version read failed (HTTP ${res.status})`);
	const table = tableFromIPC(new Uint8Array(await res.arrayBuffer()));
	const sigs = new Map<string, string>();
	for (const row of table.toArray()) {
		const r = row as Record<string, unknown>;
		sigs.set(
			String(r.id),
			rowSignature({
				label: String(r.label ?? ''),
				status: String(r.status ?? ''),
				shape: String(r.shape_type ?? ''),
				group: String(r.group ?? ''),
				text: String(r.text ?? ''),
			}),
		);
	}
	return sigs;
}

export interface RowDiff {
	added: string[]; // present now, absent in the past version
	removed: string[]; // present then, absent now
	changed: string[]; // present in both, reviewable content differs
}

/** Diff a PAST version (id→sig) against the CURRENT rows (id→sig). */
export function diffSignatures(past: Map<string, string>, current: Map<string, string>): RowDiff {
	const added: string[] = [];
	const removed: string[] = [];
	const changed: string[] = [];
	for (const [id, sig] of current) {
		if (!past.has(id)) added.push(id);
		else if (past.get(id) !== sig) changed.push(id);
	}
	for (const id of past.keys()) if (!current.has(id)) removed.push(id);
	return { added, removed, changed };
}
