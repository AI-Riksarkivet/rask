/**
 * Pure helpers for turning an upstream result set into a SQL scope clause (the
 * "WHERE" a refining Search runs under) plus hit de-duplication. No state, no
 * runes — just functions over `Hit[]`, so they're trivial to test and reuse.
 */
import { activeView, type Row } from '@rask/explorer-api/descriptor';
import type { Hit } from '@rask/explorer-api';
import { hitKey } from '$lib/utils';

/** Cap on the distinct videos a video-level refine scopes to: the `doc_id IN (…)`
 *  clause keeps at most this many (highest-ranked first), so a large upstream set
 *  can't blow up the SQL. */
const MAX_SCOPE_DOCS = 80;

/** Cap on the distinct chunks a chunk-level refine ANDs in: each becomes one
 *  one `(key1=… AND key2=… …)` OR-term over the descriptor identity fields, so we keep the clause
 *  count well under the datafusion limit that destabilises large key-OR filters. */
const MAX_SCOPE_CHUNKS = 300;

export interface ScopeClause {
	clause: string;
	/** How many videos (video scope) or chunks (chunk scope) the clause covers. */
	count: number;
	/** True when the upstream set exceeded the cap and was truncated. */
	capped: boolean;
}

/** Escape a value for inlining in a SQL single-quoted string literal. */
function sqlQuote(value: string): string {
	return `'${value.replace(/'/g, "''")}'`;
}

/** De-duplicate hits by chunk identity, preserving first-seen (rank) order. */
export function dedupeHits(hits: Hit[]): Hit[] {
	const seen = new Set<string>();
	return hits.filter((h) => {
		const k = hitKey(h);
		if (seen.has(k)) return false;
		seen.add(k);
		return true;
	});
}

/** SQL literal for a key value — quoted for strings, bare for numbers. */
function keyLiteral(value: unknown): string {
	return typeof value === 'number' ? String(value) : sqlQuote(String(value ?? ''));
}

/** `<docKey> IN (…)` over the distinct documents behind `hits` (capped at
 *  `MAX_SCOPE_DOCS`) — document-level refine: re-rank all rows in those docs.
 *  The key column is the descriptor's doc key, not a hardcoded name. */
export function videoScopeClause(hits: Hit[]): ScopeClause | null {
	const view = activeView();
	const docKey = view.docKeyField;
	const all = [...new Set(hits.map((h) => String((h as Row)[docKey] ?? '')))];
	const docs = all.slice(0, MAX_SCOPE_DOCS);
	if (!docs.length) return null;
	return {
		clause: `${docKey} IN (${docs.map(sqlQuote).join(', ')})`,
		count: docs.length,
		capped: all.length > docs.length,
	};
}

/** OR of `(k1=… AND k2=… …)` over the exact upstream rows (capped at
 *  `MAX_SCOPE_CHUNKS`) — row-level refine. The AND-terms are the descriptor's
 *  identity key fields, so any dataset's key shape works. */
export function chunkScopeClause(hits: Hit[]): ScopeClause | null {
	const view = activeView();
	const keyFields = view.keyFields;
	const uniq = dedupeHits(hits);
	const picked = uniq.slice(0, MAX_SCOPE_CHUNKS);
	if (!picked.length) return null;
	const terms = picked.map((h) => {
		const row = h as Row;
		const conds = keyFields.map((k) => `${k} = ${keyLiteral(row[k])}`);
		return `(${conds.join(' AND ')})`;
	});
	return {
		clause: `(${terms.join(' OR ')})`,
		count: picked.length,
		capped: uniq.length > picked.length,
	};
}
