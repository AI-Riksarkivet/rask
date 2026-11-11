// Medallion-tier derivation from NAMES (goal cond 3): the estate's namespaces follow the
// `[<project>-]bronze|silver|gold` convention (#84 per-tenant pages; bare stage names are the
// projectless default path). Those three are the governed tiers: raw is the external world, never a
// governed tier (R23). This is a NAMING-derived hint — the honest wording everywhere is "derived",
// never presented as a catalog fact.

export type Stage = 'bronze' | 'silver' | 'gold';

export type StageInfo = {
	stage: Stage;
	/** The tenant prefix, or null on the projectless path (bare `bronze`, `gold`, …). */
	project: string | null;
};

// Non-greedy project prefix + literal stage names, so `acme-data-silver` parses as
// project `acme-data`, stage `silver` (a greedy prefix would eat the stage).
/**
 * The identifier grammar lives in `#lib/api/identifiers.ts`, the one copy every reader shares: a second
 * copy that split on the FIRST delimiter would name the wrong namespace for a nested one. Re-exported
 * here so the stage derivation's callers import it from one place.
 */
import { leafSegment, parentNamespace } from '#lib/api/identifiers.ts';

export { parentNamespace as namespaceOfTable } from '#lib/api/identifiers.ts';

const STAGE_RE = /^(?:([a-z0-9][a-z0-9_-]*?)-)?(bronze|silver|gold)$/;

/**
 * The medallion stage a namespace encodes, or null for a non-medallion namespace.
 *
 * Takes the LAST segment of a nested id. `STAGE_RE` is anchored and `$` is outside its character
 * class, so a nested id like `acme$acme-silver` matched nothing at all and every table under a nested
 * tier namespace rendered with no stage badge — while the identical namespace one rung higher showed
 * `silver`. The stage is a property of the namespace the data sits IN, and for `acme$acme-silver` that
 * is `acme-silver`; the ancestors are tenancy, not tier.
 *
 * Ruled 2026-08-16 with the visible consequence stated: badges now appear where there were none.
 */
export function stageOf(namespace: string): StageInfo | null {
	const m = STAGE_RE.exec(leafSegment(namespace));
	if (!m) return null;
	return { stage: m[2] as Stage, project: m[1] ?? null };
}

/** The medallion stage of a table id, derived from its namespace segment. */
export function stageOfTable(table: string): StageInfo | null {
	return stageOf(parentNamespace(table));
}
